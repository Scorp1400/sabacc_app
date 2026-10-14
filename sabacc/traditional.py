"""Traditional Sabacc engine — pure game logic, no Flask, no I/O.

63-card deck: four suits (Coins and Sabers positive; Flasks and Staves
negative), ranks 1-11 plus Commander 12, Mistress 13, Master 14, Ace 15,
plus 1 suitless Idiot (worth 0) and 2 Sylops (declared 0 / +15 / -15
at showdown).

Table model (like Spike): one table = you + 3 bots picked from the
character roster. Bankrolls persist across hands. Each hand: ante 10,
deal 2, then three rounds of (shift dice, one action each, fixed-limit
betting round), then showdown. Target is +23 or -23; going over is
bombed out. No mid-hand busts — bombing is judged at showdown only,
so a bad draw can always be fixed with a negative-suit card.

Two pots: the MAIN pot (antes + bets, won every hand) and the SABACC
jackpot (seeded at 100 by the house, fed whenever everyone bombs out,
won only by Idiot's Array, Pure Sabacc, or natural +/-23, then
re-seeded to 100).

Hand hierarchy: Idiot's Array (Idiot + any 2 + any 3) > Pure Sabacc
(two Sylops) > natural +/-23 > closest to +/-23. All ties split.
"""

import random

from sabacc.spike import FALLBACK_BOTS, load_roster

SUITS = ['coins', 'flasks', 'sabers', 'staves']
NEGATIVE_SUITS = {'flasks', 'staves'}
FACES = {12: 'Commander', 13: 'Mistress', 14: 'Master', 15: 'Ace'}
SYLOP_VALUES = (0, 15, -15)

ANTE = 10
BET_UNIT = 20
RAISE_TO = 40
START_BANKROLL = 300
BOTS_PER_TABLE = 3
MAX_HAND = 5
TARGET = 23
JACKPOT_SEED = 100
SHIFT_NUDGE = 3

QUIP_KINDS = ['join_table', 'spike', 'draw', 'stand', 'fold', 'bet',
              'win', 'lose']


# ------------------------------------------------------------------ deck

def _signed(suit, rank):
    return -rank if suit in NEGATIVE_SUITS else rank


def build_deck():
    """63 cards: 4 suits x (1-11 + 4 faces), 1 Idiot, 2 Sylops."""
    deck = []
    for suit in SUITS:
        for rank in range(1, 16):
            deck.append({'suit': suit, 'rank': rank,
                         'value': _signed(suit, rank), 'kind': 'normal'})
    deck.append({'suit': None, 'rank': None, 'value': 0, 'kind': 'idiot'})
    for _ in range(2):
        deck.append({'suit': None, 'rank': None, 'value': None,
                     'kind': 'sylop'})
    return deck


def hand_total(hand):
    """Signed total; an undeclared Sylop counts as 0 for display."""
    return sum(c['value'] if c['value'] is not None else 0 for c in hand)


def is_sylop(card):
    return card['kind'] == 'sylop'


def evaluate(hand):
    """Rank a hand. Lower tier tuple = better hand.

    Returns ((tier, distance), label). Tiers: -3 Idiot's Array, -2 Pure
    Sabacc, -1 natural +/-23, 0 closest (distance to nearest target),
    1 bombed out.
    """
    kinds = [c['kind'] for c in hand]
    ranks = [c['rank'] for c in hand if c['rank'] is not None]
    if 'idiot' in kinds and 2 in ranks and 3 in ranks:
        return ((-3, 0), 'idiot_array')
    if len(hand) == 2 and all(k == 'sylop' for k in kinds):
        return ((-2, 0), 'pure_sabacc')
    t = hand_total(hand)
    if t == TARGET or t == -TARGET:
        return ((-1, 0), 'natural')
    if abs(t) > TARGET:
        return ((1, 0), 'bombed')
    return ((0, min(abs(t - TARGET), abs(t + TARGET))), 'closest')


# ------------------------------------------------------------------ table

def new_table(roster, human_bankroll=START_BANKROLL):
    """Fresh table: 3 random bots, everyone staked, jackpot seeded."""
    pool = roster if roster else FALLBACK_BOTS
    picks = random.sample(pool, min(BOTS_PER_TABLE, len(pool)))
    return {
        'human_bankroll': human_bankroll,
        'jackpot': JACKPOT_SEED,
        'button': 0,
        'bots': [{
            'id': b['id'], 'name': b['name'], 'avatar': b['avatar'],
            'quotes': b['quotes'], 'bankroll': START_BANKROLL,
            'busted': False,
        } for b in picks],
    }


def sync_table(s, table):
    """Write a finished hand's bankrolls (and jackpot) back."""
    table['human_bankroll'] = s['players'][0]['bankroll']
    for p, b in zip(s['players'][1:], table['bots']):
        b['bankroll'] = p['bankroll']
        b['busted'] = p['bankroll'] < ANTE


def table_cleared(table):
    return table['bots'] and all(b['busted'] for b in table['bots'])


# ------------------------------------------------------------------ helpers

def _mk_player(name, is_bot, bankroll, avatar, quotes, busted):
    return {'name': name, 'is_bot': is_bot, 'bankroll': bankroll,
            'avatar': avatar, 'quotes': quotes or {}, 'busted': busted,
            'hand': [], 'folded': busted, 'committed': 0, 'acted': False}


def _add_event(s, seat, text, quip=False):
    s['ev_id'] = s.get('ev_id', 0) + 1
    s['events'].append({'id': s['ev_id'], 'seat': seat, 'text': text,
                        'quip': quip})
    if len(s['events']) > 12:
        s['events'] = s['events'][-12:]


def _quip(s, seat, kind):
    p = s['players'][seat]
    qs = (p.get('quotes') or {}).get(kind)
    if qs:
        _add_event(s, seat, random.choice(qs), quip=True)


def _maybe_quip(s, seat, kind, chance):
    p = s['players'][seat]
    if p['quotes'].get(kind) and random.random() < chance:
        _quip(s, seat, kind)


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
         'dice': None, 'shift_seq': 0, 'last_shift': [],
         'bet_level': 0, 'raise_used': False, 'bet_then': 'round',
         'sylop_pick': None,
         'events': [], 'winner': None, 'jackpot_wins': 0,
         'table': table}
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
    _add_event(s, None, 'Ante %d. Two cards dealt. Target: +23 or -23.'
               % ANTE)
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
    s['last_shift'] = []
    d1 = random.randint(1, 6)
    d2 = random.randint(1, 6)
    s['dice'] = [d1, d2]
    if d1 == d2:
        s['shift_seq'] += 1
        for i in _active_seats(s):
            p = s['players'][i]
            # Only normal cards twist. Sylops have no value to nudge
            # and the Idiot is always worth 0.
            cand = [j for j, c in enumerate(p['hand'])
                    if c['kind'] == 'normal']
            if cand:
                j = random.choice(cand)
                delta = random.randint(-SHIFT_NUDGE, SHIFT_NUDGE)
                p['hand'][j]['value'] += delta
                s['last_shift'].append({'seat': i, 'idx': j,
                                        'delta': delta})
        _add_event(s, None, 'SHIFT! Double %ds. One card in every hand '
                   'twists by up to \u00b13.' % d1)
        for i in _active_seats(s):
            p = s['players'][i]
            if p['is_bot'] and p['quotes'].get('spike') \
                    and random.random() < 0.85:
                _quip(s, i, 'spike')
                break
    else:
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
        _award_main(s, [seat], 'everyone else folded')
        _add_event(s, seat, '%s takes the pot. Everyone else folded.'
                   % s['players'][seat]['name'])
        return True
    return False


def _split(s, seats, amount):
    """Split amount across seats; first seat eats the remainder."""
    n = len(seats)
    share, rem = divmod(amount, n)
    for k, seat in enumerate(seats):
        s['players'][seat]['bankroll'] += share + (rem if k == 0 else 0)
    return share


def _award_main(s, seats, reason):
    share = _split(s, seats, s['pot'])
    w = {'seats': seats,
         'names': [s['players'][seat]['name'] for seat in seats],
         'amount_each': share + (s['pot'] % len(seats) if len(seats) == 1
                                 else 0),
         'amount_total': s['pot'],
         'reason': reason,
         'jackpot': None}
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
    """Draw / swap / stand / fold during the action rounds. Hands cap
    at MAX_HAND cards; past that it's swap-only."""
    if s['phase'] != 'actions':
        raise ValueError('not in the action rounds')
    if s['turn'] != seat:
        raise ValueError('not your turn')
    p = s['players'][seat]
    if p['folded'] or p['busted']:
        raise ValueError('you are not in this hand')
    if action == 'draw':
        if len(p['hand']) >= MAX_HAND:
            raise ValueError('hand is full — swap or stand')
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


# ------------------------------------------------------------------ showdown

def _bot_sylop_value(hand, card):
    """Which of 0/+15/-15 gives the bot its best hand?"""
    best_v, best_rank = 0, None
    for v in SYLOP_VALUES:
        card['value'] = v
        rank, _ = evaluate(hand)
        card['value'] = None
        if best_rank is None or rank < best_rank:
            best_rank, best_v = rank, v
    return best_v


def _undeclared_sylops(hand):
    return [c for c in hand if c['kind'] == 'sylop'
            and c['value'] is None]


def _needs_sylop_pick(s):
    """Human has a Sylop to declare and it isn't an automatic Pure."""
    if 0 not in _active_seats(s):
        return False
    hand = s['players'][0]['hand']
    if len(hand) == 2 and all(c['kind'] == 'sylop' for c in hand):
        return False  # Pure Sabacc — no pick needed
    return bool(_undeclared_sylops(hand))


def _showdown(s):
    """Bots declare their Sylops instantly; the human picks via the
    'sylop' phase if they hold one."""
    for i in _active_seats(s):
        p = s['players'][i]
        if not p['is_bot']:
            continue
        for c in p['hand']:
            if c['kind'] == 'sylop' and c['value'] is None:
                c['value'] = _bot_sylop_value(p['hand'], c)
    if _needs_sylop_pick(s):
        s['phase'] = 'sylop'
        s['turn'] = None
        base = hand_total([c for c in s['players'][0]['hand']
                           if c['value'] is not None])
        s['sylop_pick'] = {
            'options': [{'value': v, 'total': base + v}
                        for v in SYLOP_VALUES]}
        _add_event(s, 0, 'Your Sylop awaits its value: 0, +15, or -15.')
        return s
    _resolve_showdown(s)


def do_sylop(s, seat, value):
    """Human declares a Sylop's value at showdown."""
    if s['phase'] != 'sylop':
        raise ValueError('no Sylop to declare')
    if seat != 0:
        raise ValueError('not your Sylop')
    if value not in SYLOP_VALUES:
        raise ValueError('a Sylop is 0, +15, or -15')
    hand = s['players'][0]['hand']
    todo = _undeclared_sylops(hand)
    if not todo:
        raise ValueError('no Sylop to declare')
    todo[0]['value'] = value
    _add_event(s, 0, 'Your Sylop resolves as %+d.' % value)
    if _needs_sylop_pick(s):
        base = hand_total([c for c in hand if c['value'] is not None])
        s['sylop_pick'] = {
            'options': [{'value': v, 'total': base + v}
                        for v in SYLOP_VALUES]}
        return s
    s['sylop_pick'] = None
    _resolve_showdown(s)
    return s


def _resolve_showdown(s):
    scored = []
    for i in _active_seats(s):
        rank, label = evaluate(s['players'][i]['hand'])
        scored.append((rank, label, i))
    scored.sort(key=lambda x: x[0])
    table = s['table']
    if scored[0][1] == 'bombed':
        # Everyone bombed: the main pot feeds the Sabacc jackpot.
        table['jackpot'] += s['pot']
        _add_event(s, None, 'Everyone bombed out! The main pot of %d '
                   'feeds the Sabacc jackpot (now %d).'
                   % (s['pot'], table['jackpot']))
        s['pot'] = 0
        s['phase'] = 'done'
        s['turn'] = None
        s['winner'] = {'seats': [], 'names': [], 'amount_each': 0,
                       'amount_total': 0, 'reason': 'everyone_bombed',
                       'jackpot': None, 'rank_label': 'bombed'}
        return s
    best = scored[0][0]
    winners = [x for x in scored if x[0] == best]
    seats = [x[2] for x in winners]
    label = winners[0][1]
    w = _award_main(s, seats, label)
    w['rank_label'] = label
    w['winning_total'] = hand_total(s['players'][seats[0]]['hand'])
    # The jackpot cracks only for the holy hands.
    jt = [x for x in winners if x[0][0] <= -1]
    if jt:
        jseats = [x[2] for x in jt]
        amount = table['jackpot']
        share = _split(s, jseats, amount)
        w['jackpot'] = {'seats': jseats,
                        'names': [s['players'][x]['name'] for x in jseats],
                        'amount_each': share,
                        'amount_total': amount}
        table['jackpot'] = JACKPOT_SEED
        s['jackpot_wins'] += 1
        _add_event(s, jseats[0], 'JACKPOT! %s cracks the Sabacc pot '
                   'for %d!' % (w['jackpot']['names'][0], amount))
    _add_event(s, seats[0], '%s wins %d (%s%s).'
               % (w['names'][0], w['amount_total'], label.replace('_', ' '),
                  ' + JACKPOT %d' % w['jackpot']['amount_total']
                  if w['jackpot'] else ''))
    return s


# ------------------------------------------------------------------ turn pump

def step_bot(s):
    """Advance exactly one bot turn. Returns True if a bot acted."""
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

def _target(total):
    return TARGET if total >= 0 else -TARGET


def _dist(total):
    return min(abs(total - TARGET), abs(total + TARGET))


def _worst_idx(hand, target):
    """Card hurting the total most relative to the target: the
    biggest value when over, the smallest when under."""
    t = hand_total(hand)
    if t > target:
        return max(range(len(hand)), key=lambda i: hand[i]['value'] or 0)
    if t < target:
        return min(range(len(hand)), key=lambda i: hand[i]['value'] or 0)
    return random.randrange(len(hand))


def bot_action(s, seat):
    """Pick a legal action-round move. Aim at the nearer 23."""
    p = s['players'][seat]
    t = hand_total(p['hand'])
    target = _target(t)
    d = _dist(t)
    n = len(p['hand'])
    bombed = abs(t) > TARGET
    r = random.random()
    if bombed:
        # No mid-hand elimination: draw back toward the target.
        if n < MAX_HAND:
            return ('draw', None)
        return ('swap', _worst_idx(p['hand'], target))
    if d <= 2:
        return ('stand', None)
    if n >= MAX_HAND:
        return ('stand', None) if d <= 4 \
            else ('swap', _worst_idx(p['hand'], target))
    if d >= 8:
        if r < 0.75:
            return ('draw', None)
        return ('swap', _worst_idx(p['hand'], target))
    if r < 0.55:
        return ('draw', None)
    if r < 0.85:
        return ('swap', _worst_idx(p['hand'], target))
    return ('stand', None)


def bot_bet(s, seat):
    """Pick a legal betting move from the distance to 23."""
    p = s['players'][seat]
    d = _dist(hand_total(p['hand']))
    bombed = abs(hand_total(p['hand'])) > TARGET
    to_call = s['bet_level'] - p['committed']
    r = random.random()
    if to_call == 0:
        if d <= 1 and r < 0.35:
            return 'bet'
        if d <= 3 and r < 0.15:
            return 'bet'
        return 'check'
    if d <= 1 and not s['raise_used'] and r < 0.35:
        return 'raise'
    if d <= 4:
        return 'call'
    if bombed:
        return 'call' if r < 0.3 else 'fold'
    if d >= 9:
        return 'call' if r < 0.4 else 'fold'
    return 'call' if r < 0.7 else 'fold'


def legal_actions(s, seat):
    p = s['players'][seat]
    if s['phase'] == 'actions' and s['turn'] == seat \
            and not p['folded'] and not p['busted']:
        acts = ['swap', 'stand', 'fold']
        if len(p['hand']) < MAX_HAND:
            acts.insert(0, 'draw')
        return acts
    return []


def display_bets(s, seat):
    """Betting options ignoring turn — for rendering buttons
    (possibly disabled) on every tick. Validation uses legal_bets."""
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

def _ncards(s):
    return (len(s['deck']) + len(s['discards'])
            + sum(len(p['hand']) for p in s['players']))


def _play_hand(s):
    """Bot heuristics for every seat; seat 0 picks Sylops like a
    good player."""
    guard = 0
    while s['phase'] not in ('done', 'busted', 'cleared') and guard < 5000:
        guard += 1
        if s['phase'] == 'sylop':
            opts = s['sylop_pick']['options']
            hand = s['players'][0]['hand']
            best_v, best_rank = opts[0]['value'], None
            for o in opts:
                for c in hand:
                    if c['kind'] == 'sylop' and c['value'] is None:
                        c['value'] = o['value']
                        rank, _ = evaluate(hand)
                        c['value'] = None
                        if best_rank is None or rank < best_rank:
                            best_rank, best_v = rank, o['value']
                        break
            do_sylop(s, 0, best_v)
            continue
        seat = s['turn']
        if seat is None:
            break
        if s['phase'] == 'actions':
            a, idx = bot_action(s, seat)
            try:
                do_action(s, seat, a, idx)
            except ValueError:
                do_action(s, seat, 'stand')
        elif s['phase'] == 'betting':
            do_bet(s, seat, bot_bet(s, seat))
        else:
            break
    return s


def selftest_tables(n=25, max_hands=60):
    """Full tables: persistent bankrolls + jackpot, busts, clears."""
    for t in range(n):
        table = new_table([])
        assert table['jackpot'] == JACKPOT_SEED
        hands = 0
        wins = 0
        while hands < max_hands:
            hands += 1
            s = new_hand(table, button=hands % 4)
            if s['phase'] in ('busted', 'cleared'):
                break
            s = _play_hand(s)
            wins += s['jackpot_wins']
            assert s['phase'] == 'done', 'table %d hand %d stuck in %s' \
                % (t, hands, s['phase'])
            assert s['pot'] == 0, 'table %d: main pot not paid' % t
            assert s['winner'] is not None, 'table %d: no winner' % t
            sync_table(s, table)
            for p in s['players']:
                assert p['bankroll'] >= 0, 'table %d: negative bankroll' % t
            # Money: player money + pots == buy-ins + house jackpot seeds.
            injected = JACKPOT_SEED * (1 + wins)
            total = (table['human_bankroll']
                     + sum(b['bankroll'] for b in table['bots'])
                     + table['jackpot'])
            assert total == 4 * START_BANKROLL + injected, \
                'table %d: money leaked (%d vs %d)' % (t, total,
                       4 * START_BANKROLL + injected)
            assert _ncards(s) == 63, 'table %d: cards lost (%d)' \
                % (t, _ncards(s))
            if table_cleared(table):
                break
        table['human_bankroll'] = 0
        s = new_hand(table)
        assert s['phase'] == 'busted', 'bust-out did not trigger'
    print('traditional table selftest OK: %d tables, money conserved, '
          '63 cards accounted for' % n)


def selftest_hierarchy():
    def C(suit, rank):
        return {'suit': suit, 'rank': rank,
                'value': _signed(suit, rank), 'kind': 'normal'}
    idiot = {'suit': None, 'rank': None, 'value': 0, 'kind': 'idiot'}
    sylop = {'suit': None, 'rank': None, 'value': None, 'kind': 'sylop'}
    arr = [idiot, C('coins', 2), C('sabers', 3)]
    pure = [dict(sylop), dict(sylop)]
    nat = [C('coins', 11), C('sabers', 12)]          # +23
    nat_neg = [C('flasks', 11), C('staves', 12)]     # -23
    close = [C('coins', 11), C('coins', 11)]         # +22
    assert evaluate(arr)[0] < evaluate(pure)[0] < evaluate(nat)[0]
    assert evaluate(nat)[0] == evaluate(nat_neg)[0]
    assert evaluate(nat)[0] < evaluate(close)[0]
    assert evaluate([C('coins', 15), C('sabers', 15)])[1] == 'bombed'
    # ties: +22 vs -22 -> identical rank -> split
    assert evaluate([C('coins', 11), C('coins', 11)])[0] == \
        evaluate([C('flasks', 11), C('flasks', 11)])[0]
    # Idiot needs the 2 AND the 3
    assert evaluate([idiot, C('coins', 2), C('coins', 5)])[1] != \
        'idiot_array'
    print('traditional hierarchy selftest OK')


def selftest_sylop_flow():
    """Human holds a Sylop: picker phase appears, pick resolves."""
    table = new_table([])
    s = new_hand(table)
    # rig: give seat 0 a Sylop + a 10, jump to showdown
    s['players'][0]['hand'] = [
        {'suit': None, 'rank': None, 'value': None, 'kind': 'sylop'},
        {'suit': 'coins', 'rank': 10, 'value': 10, 'kind': 'normal'}]
    for p in s['players'][1:]:
        p['folded'] = True
    _showdown(s)
    assert s['phase'] == 'sylop', 'expected sylop phase, got %s' % s['phase']
    opts = s['sylop_pick']['options']
    assert [o['value'] for o in opts] == [0, 15, -15]
    assert [o['total'] for o in opts] == [10, 25, -5]
    do_sylop(s, 0, -15)
    assert s['phase'] == 'done'
    assert hand_total(s['players'][0]['hand']) == -5
    # bad values rejected
    s2 = new_hand(table)
    s2['players'][0]['hand'] = [
        {'suit': None, 'rank': None, 'value': None, 'kind': 'sylop'},
        {'suit': 'coins', 'rank': 10, 'value': 10, 'kind': 'normal'}]
    for p in s2['players'][1:]:
        p['folded'] = True
    _showdown(s2)
    try:
        do_sylop(s2, 0, 7)
        raise AssertionError('bad sylop value accepted')
    except ValueError:
        pass
    print('traditional sylop-flow selftest OK')


def selftest_everyone_bombed():
    """Everyone over 23: main pot feeds the jackpot, no winner."""
    table = new_table([])
    s = new_hand(table)
    for p in s['players']:
        p['hand'] = [
            {'suit': 'coins', 'rank': 15, 'value': 15, 'kind': 'normal'},
            {'suit': 'sabers', 'rank': 15, 'value': 15, 'kind': 'normal'}]
    pot_before = s['pot']
    jack_before = table['jackpot']
    _resolve_showdown(s)
    assert s['phase'] == 'done'
    assert s['winner']['reason'] == 'everyone_bombed'
    assert s['pot'] == 0
    assert table['jackpot'] == jack_before + pot_before
    print('traditional everyone-bombed selftest OK')


def selftest(n=200):
    for h in range(n):
        table = new_table([])
        s = new_hand(table, button=h % 4)
        s = _play_hand(s)
        assert s['phase'] == 'done', 'hand %d stuck in %s' % (h, s['phase'])
        assert s['pot'] == 0, 'hand %d: pot not paid' % h
    print('traditional selftest OK: %d hands, no crashes' % n)


if __name__ == '__main__':
    assert len(build_deck()) == 63
    suits = [c['suit'] for c in build_deck() if c['kind'] == 'normal']
    assert all(suits.count(x) == 15 for x in SUITS)
    assert sum(1 for c in build_deck() if c['kind'] == 'sylop') == 2
    assert sum(1 for c in build_deck() if c['kind'] == 'idiot') == 1
    selftest_hierarchy()
    selftest_sylop_flow()
    selftest_everyone_bombed()
    selftest()
    selftest_tables()
