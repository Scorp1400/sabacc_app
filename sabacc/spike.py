"""Corellian Spike sabacc engine — pure game logic, no Flask, no I/O.

62-card deck: three staves (circle, triangle, square), each with
+1..+10 and -1..-10, plus 2 Sylops (value 0).

Table model (v2): one table = you + 3 bots picked from the character
roster. Bankrolls persist across hands. Each hand: ante 10, then three
rounds of (spike dice, one action each, fixed-limit betting round),
then showdown. Closest total to 0 wins; exact 0 is a Spike.

A player whose bankroll drops below the ante is busted and sits out.
Bust all three bots to clear the table. No side pots: bets are capped
at the bankroll and the whole pot goes to the winner.
"""

import json
import random
from pathlib import Path

STAVES = ['\u25cb', '\u25b3', '\u25a1']  # ○ △ □
ANTE = 10
BET_UNIT = 20
RAISE_TO = 40
START_BANKROLL = 300
BOTS_PER_TABLE = 3

QUIP_KINDS = ['join_table', 'spike', 'draw', 'stand', 'fold', 'bet',
              'win', 'lose']

FALLBACK_BOTS = [
    {'id': 'vex', 'name': 'Vex', 'avatar': None, 'quotes': {}},
    {'id': 'mira', 'name': 'Mira', 'avatar': None, 'quotes': {}},
    {'id': 'rook', 'name': 'Rook', 'avatar': None, 'quotes': {}},
]


# ------------------------------------------------------------------ roster

def load_roster(char_dir):
    """Read characters/<id>.json into bot dicts. Empty list if missing."""
    roster = []
    p = Path(char_dir)
    if not p.is_dir():
        return roster
    for f in sorted(p.glob('*.json')):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        quotes = d.get('quotes') or {}
        roster.append({
            'id': str(d.get('id', f.stem)),
            'name': str(d.get('name', f.stem.title())),
            'avatar': d.get('avatar'),
            'quotes': {k: list(quotes.get(k) or []) for k in QUIP_KINDS},
        })
    return roster


def new_table(roster, human_bankroll=START_BANKROLL):
    """Fresh table: 3 random bots, everyone staked."""
    pool = roster if roster else FALLBACK_BOTS
    picks = random.sample(pool, min(BOTS_PER_TABLE, len(pool)))
    return {
        'human_bankroll': human_bankroll,
        'button': 0,
        'bots': [{
            'id': b['id'], 'name': b['name'], 'avatar': b['avatar'],
            'quotes': b['quotes'], 'bankroll': START_BANKROLL,
            'busted': False,
        } for b in picks],
    }


def sync_table(s, table):
    """Write a finished hand's bankrolls back to the table."""
    table['human_bankroll'] = s['players'][0]['bankroll']
    for p, b in zip(s['players'][1:], table['bots']):
        b['bankroll'] = p['bankroll']
        b['busted'] = p['bankroll'] < ANTE


def table_cleared(table):
    return table['bots'] and all(b['busted'] for b in table['bots'])


# ------------------------------------------------------------------ helpers

def build_deck():
    """62 cards: 3 staves x (+1..+10, -1..-10), plus 2 Sylops."""
    deck = []
    for s in STAVES:
        for v in range(1, 11):
            deck.append({'stave': s, 'value': v})
            deck.append({'stave': s, 'value': -v})
    deck.append({'stave': None, 'value': 0})
    deck.append({'stave': None, 'value': 0})
    return deck


def hand_total(hand):
    return sum(c['value'] for c in hand)


def is_sylop(card):
    return card['stave'] is None


def _mk_player(name, is_bot, bankroll, avatar, quotes, busted):
    return {'name': name, 'is_bot': is_bot, 'bankroll': bankroll,
            'avatar': avatar, 'quotes': quotes or {}, 'busted': busted,
            'hand': [], 'folded': busted, 'committed': 0, 'acted': False}


def _add_event(s, seat, text, quip=False):
    s['ev_id'] = s.get('ev_id', 0) + 1
    s['events'].append({'id': s['ev_id'], 'seat': seat, 'text': text, 'quip': quip})
    if len(s['events']) > 12:
        s['events'] = s['events'][-12:]


def _quip(s, seat, kind):
    p = s['players'][seat]
    qs = (p.get('quotes') or {}).get(kind)
    if qs:
        _add_event(s, seat, random.choice(qs), quip=True)


def _ensure_deck(s, n):
    if len(s['deck']) < n and s['discards']:
        disc = s['discards']
        random.shuffle(disc)
        s['deck'].extend(disc)
        s['discards'] = []


def _draw_card(s):
    _ensure_deck(s, 1)
    if not s['deck']:
        return None
    return s['deck'].pop()


def _deal_to_active(s, n):
    for i in _active_seats(s):
        p = s['players'][i]
        for _ in range(n):
            c = _draw_card(s)
            if c is not None:
                p['hand'].append(c)


def _active_seats(s):
    return [i for i, p in enumerate(s['players'])
            if not p['folded'] and not p['busted']]


def _first_to_act(s):
    n = len(s['players'])
    for i in range(n):
        seat = (s['button'] + 1 + i) % n
        p = s['players'][seat]
        if not p['folded'] and not p['busted']:
            return seat
    return None


def _next_seat(s):
    if s['turn'] is None:
        return None
    n = len(s['players'])
    for i in range(1, n + 1):
        seat = (s['turn'] + i) % n
        p = s['players'][seat]
        if not p['folded'] and not p['busted'] and not p['acted']:
            return seat
    return None


def _reset_acted(s, skip=None):
    for i in _active_seats(s):
        if i != skip:
            s['players'][i]['acted'] = False


# ------------------------------------------------------------------ flow

def new_hand(table, button=0):
    """Deal a hand on a table. Busts are resolved from bankrolls first.

    Returns a state with phase 'busted' if the human can't cover the
    ante, or 'cleared' if every bot is busted.
    """
    human_busted = table['human_bankroll'] < ANTE
    for b in table['bots']:
        b['busted'] = b['bankroll'] < ANTE
    players = [_mk_player('You', False, table['human_bankroll'],
                          None, None, human_busted)]
    for b in table['bots']:
        players.append(_mk_player(b['name'], True, b['bankroll'],
                                  b['avatar'], b['quotes'], b['busted']))
    s = {'players': players, 'deck': [], 'discards': [],
         'pot': 0, 'round': 0, 'phase': 'actions',
         'button': button % max(1, len(players)), 'turn': None,
         'dice': None, 'last_spike': False, 'spike_seq': 0,
         'bet_level': 0, 'raise_used': False, 'bet_then': 'round',
         'events': [], 'winner': None, 'table': table}
    if human_busted:
        s['phase'] = 'busted'
        _add_event(s, None, 'You are busted. Buy back in to keep playing.')
        return s
    if not any(not p['busted'] for p in players[1:]):
        s['phase'] = 'cleared'
        _add_event(s, None, 'Every bot is busted. Table cleared!')
        return s
    deck = build_deck()
    random.shuffle(deck)
    s['deck'] = deck
    for i in _active_seats(s):
        p = s['players'][i]
        amt = min(ANTE, p['bankroll'])
        p['bankroll'] -= amt
        s['pot'] += amt
    _deal_to_active(s, 2)
    _add_event(s, None, 'Ante %d. Two cards dealt.' % ANTE)
    # one bot greets the table
    greeters = [i for i in _active_seats(s)
                if s['players'][i]['is_bot']
                and s['players'][i]['quotes'].get('join_table')]
    if greeters and random.random() < 0.85:
        _quip(s, random.choice(greeters), 'join_table')
    _start_round(s)
    return s


def _start_round(s):
    s['round'] += 1
    s['phase'] = 'actions'
    d1 = random.randint(1, 6)
    d2 = random.randint(1, 6)
    s['dice'] = [d1, d2]
    if d1 == d2:
        s['last_spike'] = True
        s['spike_seq'] += 1
        for i in _active_seats(s):
            p = s['players'][i]
            s['discards'].extend(p['hand'])
            p['hand'] = []
        _deal_to_active(s, 2)
        _add_event(s, None, 'SPIKE! Double %ds. Every hand re-dealt.' % d1)
        for i in _active_seats(s):
            p = s['players'][i]
            if p['is_bot'] and p['quotes'].get('spike') \
                    and random.random() < 0.85:
                _quip(s, i, 'spike')
                break
    else:
        s['last_spike'] = False
        _add_event(s, None, 'Round %d of 3. Dice show %d and %d.'
                   % (s['round'], d1, d2))
    _reset_acted(s)
    s['turn'] = _first_to_act(s)


def _commit(s, seat, amt):
    p = s['players'][seat]
    amt = max(0, min(amt, p['bankroll']))
    p['bankroll'] -= amt
    p['committed'] += amt
    s['pot'] += amt
    return amt


def _check_early_win(s):
    act = _active_seats(s)
    if len(act) == 1:
        seat = act[0]
        _award(s, [seat], 'everyone else folded')
        _add_event(s, seat, '%s takes the pot. Everyone else folded.'
                   % s['players'][seat]['name'])
        return True
    return False


def _award(s, seats, reason):
    n = len(seats)
    share, rem = divmod(s['pot'], n)
    for k, seat in enumerate(seats):
        s['players'][seat]['bankroll'] += share + (rem if k == 0 else 0)
    w = {'seats': seats,
         'names': [s['players'][seat]['name'] for seat in seats],
         'amount_each': share + (rem if n == 1 else 0),
         'amount_total': s['pot'],
         'reason': reason,
         'pot': s['pot']}
    s['pot'] = 0
    s['phase'] = 'done'
    s['turn'] = None
    s['winner'] = w
    for i in seats:
        p = s['players'][i]
        if p['is_bot'] and p['quotes'].get('win'):
            _quip(s, i, 'win')
            break
    return w


def do_action(s, seat, action, idx=None):
    """Draw / swap / stand / fold during the action rounds."""
    if s['phase'] != 'actions':
        raise ValueError('not in the action rounds')
    if s['turn'] != seat:
        raise ValueError('not your turn')
    p = s['players'][seat]
    if p['folded'] or p['busted']:
        raise ValueError('you are not in this hand')
    if action == 'draw':
        c = _draw_card(s)
        if c is None:
            _add_event(s, seat, '%s wants a card, but the deck is empty.'
                       % p['name'])
        else:
            p['hand'].append(c)
            _add_event(s, seat, '%s draws a card.' % p['name'])
            if p['is_bot']:
                _maybe_quip(s, seat, 'draw', 0.45)
    elif action == 'swap':
        if idx is None or not 0 <= idx < len(p['hand']):
            raise ValueError('pick a card to swap')
        old = p['hand'].pop(idx)
        s['discards'].append(old)
        c = _draw_card(s)
        if c is not None:
            p['hand'].append(c)
        _add_event(s, seat, '%s swaps a card.' % p['name'])
    elif action == 'stand':
        _add_event(s, seat, '%s stands pat.' % p['name'])
        if p['is_bot']:
            _maybe_quip(s, seat, 'stand', 0.4)
    elif action == 'fold':
        p['folded'] = True
        s['discards'].extend(p['hand'])
        p['hand'] = []
        _add_event(s, seat, '%s folds.' % p['name'])
        _quip(s, seat, 'fold')
    else:
        raise ValueError('unknown action')
    p['acted'] = True
    if _check_early_win(s):
        return s
    nxt = _next_seat(s)
    if nxt is None:
        _start_betting(s, 'round' if s['round'] < 3 else 'showdown')
    else:
        s['turn'] = nxt
    return s


def _maybe_quip(s, seat, kind, chance):
    p = s['players'][seat]
    if p['quotes'].get(kind) and random.random() < chance:
        _quip(s, seat, kind)


def _start_betting(s, then):
    s['phase'] = 'betting'
    s['bet_then'] = then
    s['bet_level'] = 0
    s['raise_used'] = False
    for i in _active_seats(s):
        s['players'][i]['committed'] = 0
        s['players'][i]['acted'] = False
    s['turn'] = _first_to_act(s)
    _add_event(s, None, 'Betting round. Check, bet %d, or fold.' % BET_UNIT)


def do_bet(s, seat, action):
    """check / bet / call / raise / fold. Fixed limit, one raise max."""
    if s['phase'] != 'betting':
        raise ValueError('not in the betting round')
    if s['turn'] != seat:
        raise ValueError('not your turn')
    p = s['players'][seat]
    if p['folded'] or p['busted']:
        raise ValueError('you are not in this hand')
    to_call = s['bet_level'] - p['committed']
    if action == 'check':
        if to_call != 0:
            raise ValueError('there is a bet to call')
        _add_event(s, seat, '%s checks.' % p['name'])
    elif action == 'bet':
        if s['bet_level'] != 0:
            raise ValueError('there is already a bet')
        got = _commit(s, seat, BET_UNIT)
        s['bet_level'] = BET_UNIT
        _reset_acted(s, skip=seat)
        _add_event(s, seat, '%s bets %d.' % (p['name'], got))
        if p['is_bot']:
            _maybe_quip(s, seat, 'bet', 0.65)
    elif action == 'call':
        if to_call <= 0:
            raise ValueError('nothing to call')
        got = _commit(s, seat, to_call)
        _add_event(s, seat, '%s calls %d.' % (p['name'], got))
    elif action == 'raise':
        if s['raise_used'] or to_call <= 0:
            raise ValueError('cannot raise now')
        got = _commit(s, seat, RAISE_TO - p['committed'])
        s['bet_level'] = RAISE_TO
        s['raise_used'] = True
        _reset_acted(s, skip=seat)
        _add_event(s, seat, '%s raises to %d.' % (p['name'], RAISE_TO))
        if p['is_bot']:
            _maybe_quip(s, seat, 'bet', 0.75)
    elif action == 'fold':
        p['folded'] = True
        s['discards'].extend(p['hand'])
        p['hand'] = []
        _add_event(s, seat, '%s folds.' % p['name'])
        _quip(s, seat, 'fold')
    else:
        raise ValueError('unknown bet action')
    p['acted'] = True
    if _check_early_win(s):
        return s
    nxt = _next_seat(s)
    if nxt is None:
        if s['bet_then'] == 'round':
            _start_round(s)
        else:
            _showdown(s)
    else:
        s['turn'] = nxt
    return s


def _showdown(s):
    act = _active_seats(s)
    scored = []
    for i in act:
        t = hand_total(s['players'][i]['hand'])
        scored.append((abs(t), -len(s['players'][i]['hand']), i, t))
    scored.sort()
    best_abs, best_neglen, _, best_t = scored[0]
    tied = [x for x in scored if x[0] == best_abs and x[1] == best_neglen]
    seats = [x[2] for x in tied]
    is_spike = (best_t == 0)
    reason = 'spike' if is_spike else 'showdown'
    w = _award(s, seats, reason)
    w['total'] = best_t
    w['is_spike'] = is_spike
    w['hand'] = [dict(c) for c in s['players'][seats[0]]['hand']]
    if is_spike:
        _add_event(s, seats[0], 'SPIKE! %s hits exactly zero and takes %d.'
                   % (w['names'][0], w['amount_total']))
    else:
        _add_event(s, seats[0], '%s wins %d with a total of %+d.'
                   % (w['names'][0], w['amount_total'], best_t))
    return s


# ------------------------------------------------------------------ turn pump

def step_bot(s):
    """Advance exactly one bot turn. Returns True if a bot acted.

    The frontend calls this once per bot turn (with a thinking pause
    between) so turns feel live instead of resolving instantly.
    """
    if s['phase'] not in ('actions', 'betting'):
        return False
    seat = s['turn']
    if seat is None or seat == 0:
        return False
    p = s['players'][seat]
    if p['folded'] or p['busted']:
        return False
    if s['phase'] == 'actions':
        act, idx = bot_action(s, seat)
        do_action(s, seat, act, idx)
    else:
        do_bet(s, seat, bot_bet(s, seat))
    return True


# ------------------------------------------------------------------ bot AI

def _worst_idx(hand):
    """Index of the card hurting the total most (furthest from zero
    in the direction of the total's sign)."""
    t = hand_total(hand)
    if t > 0:
        return max(range(len(hand)), key=lambda i: hand[i]['value'])
    if t < 0:
        return min(range(len(hand)), key=lambda i: hand[i]['value'])
    return random.randrange(len(hand))


def bot_action(s, seat):
    """Pick a legal action-round move. Dumb but sensible."""
    p = s['players'][seat]
    t = hand_total(p['hand'])
    a = abs(t)
    r = random.random()
    if a <= 3:
        return ('stand', None)
    if a >= 8:
        if a >= 12 and r < 0.25:
            return ('fold', None)
        if r < 0.75:
            return ('draw', None)
        return ('swap', _worst_idx(p['hand']))
    if r < 0.12:
        return ('draw', None)
    return ('swap', _worst_idx(p['hand']))


def bot_bet(s, seat):
    """Pick a legal betting move."""
    p = s['players'][seat]
    a = abs(hand_total(p['hand']))
    to_call = s['bet_level'] - p['committed']
    r = random.random()
    if to_call == 0:
        if a == 0 and r < 0.35:
            return 'bet'
        if a <= 3 and r < 0.15:
            return 'bet'
        return 'check'
    if a == 0 and not s['raise_used'] and r < 0.35:
        return 'raise'
    if a <= 4:
        return 'call'
    if a >= 9:
        return 'call' if r < 0.4 else 'fold'
    return 'call' if r < 0.7 else 'fold'


def legal_actions(s, seat):
    p = s['players'][seat]
    if s['phase'] == 'actions' and s['turn'] == seat \
            and not p['folded'] and not p['busted']:
        return ['draw', 'swap', 'stand', 'fold']
    return []


def display_bets(s, seat):
    """Human's betting options ignoring turn — for rendering buttons
    (possibly disabled) on every tick. Validation still uses legal_bets."""
    p = s['players'][seat]
    if s['phase'] != 'betting' or p['busted']:
        return []
    to_call = max(0, s['bet_level'] - p['committed'])
    acts = ['fold']
    if to_call == 0:
        acts += ['check', 'bet']
    else:
        acts += ['call']
        if not s['raise_used']:
            acts += ['raise']
    return acts


def legal_bets(s, seat):
    p = s['players'][seat]
    if s['turn'] != seat or p['folded']:
        return []
    return display_bets(s, seat)


# ------------------------------------------------------------------ selftest

def _play_hand(s):
    guard = 0
    while s['phase'] not in ('done', 'busted', 'cleared') and guard < 2000:
        guard += 1
        seat = s['turn']
        if seat is None:
            break
        if s['phase'] == 'actions':
            a, idx = bot_action(s, seat)
            do_action(s, seat, a, idx)
        elif s['phase'] == 'betting':
            do_bet(s, seat, bot_bet(s, seat))
        else:
            break
    return s


def selftest_tables(n=25, max_hands=80):
    """Full tables: persistent bankrolls, busts, table clears."""
    cleared = 0
    for t in range(n):
        table = new_table([])
        hands = 0
        while hands < max_hands:
            hands += 1
            s = new_hand(table, button=hands % 4)
            if s['phase'] in ('busted', 'cleared'):
                break
            s = _play_hand(s)
            assert s['phase'] == 'done', 'table %d hand %d stuck' % (t, hands)
            assert s['pot'] == 0, 'table %d: pot not paid' % t
            sync_table(s, table)
            total = table['human_bankroll'] + sum(
                b['bankroll'] for b in table['bots'])
            assert total == 4 * START_BANKROLL, \
                'table %d: money leaked (%d)' % (t, total)
            ncards = (len(s['deck']) + len(s['discards'])
                      + sum(len(p['hand']) for p in s['players']))
            assert ncards == 62, 'table %d: cards lost' % t
            if table_cleared(table):
                cleared += 1
                break
        # human bust-out path must also terminate cleanly
        table['human_bankroll'] = 0
        s = new_hand(table)
        assert s['phase'] == 'busted', 'bust-out did not trigger'
    print('spike table selftest OK: %d tables, %d cleared by busts, '
          'money conserved, 62 cards accounted for' % (n, cleared))


def selftest(n=200):
    for h in range(n):
        table = new_table([])
        s = new_hand(table, button=h % 4)
        s = _play_hand(s)
        assert s['phase'] == 'done', 'hand %d did not finish' % h
        assert s['pot'] == 0, 'hand %d: pot not paid out' % h
        assert s['winner'] is not None, 'hand %d: no winner' % h
    print('spike selftest OK: %d hands, no crashes' % n)


if __name__ == '__main__':
    build_deck()
    assert len(build_deck()) == 62
    assert hand_total([{'stave': None, 'value': 0}]) == 0
    selftest()
    selftest_tables()
