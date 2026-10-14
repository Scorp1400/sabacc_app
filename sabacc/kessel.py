"""Kessel Sabacc engine — pure game logic, no Flask, no I/O.

16-card deck: Sand 1-6 (yellow), Blood 1-6 (red), 2 Sylops, 2 Impostors.
4 players: you + 3 bots. Each round is 3 turns per player, then showdown.
Lowest pair wins (pair of 1s best). No pair -> smallest card gap wins.
Sylop copies the other card's final value (instant pair). Two Sylops =
Pure Sabacc, beats everything. Impostor value = pick of 2 dice rolled
at showdown (3 dice with the Loaded Die token). The player picks the
die; bots auto-pick the die that gives the best hand.

Chips: 4 each per game. Draw costs 1 chip. At showdown, losers pay
their card gap in chips (a losing pair pays 0); winners regain the
chips they spent on draws that round. 0 chips after settlement =
eliminated. Last player with chips wins the game and takes the
credit pot (4x the stake).

Shift tokens: draft 3 of 12 (or none) pre-game, one use each per game,
played at the start of your turn (before Draw/Stand), one token per
turn. Credits are handled by the app; this engine only tracks chips.
"""

import random

from sabacc.spike import FALLBACK_BOTS, load_roster

STAKE_TIERS = [50, 100, 200]
START_CHIPS = 4
TURNS_PER_ROUND = 3
BOTS_PER_TABLE = 3

TOKEN_POOL = [
    {'id': 'free_draw', 'name': 'Free Draw',
     'desc': 'Your next draw is free.', 'target': 'none'},
    {'id': 'tariff', 'name': 'Tariff',
     'desc': 'All opponents lose 1 chip each to you.', 'target': 'none'},
    {'id': 'embargo', 'name': 'Embargo',
     'desc': 'Target opponent skips their next turn.',
     'target': 'opponent'},
    {'id': 'rebate', 'name': 'Rebate',
     'desc': 'Regain 2 chips.', 'target': 'none'},
    {'id': 'peek', 'name': 'Peek',
     'desc': "See one opponent's hand.", 'target': 'opponent'},
    {'id': 'shield', 'name': 'Shield',
     'desc': 'Immune to Tariff and Embargo for a round.',
     'target': 'none'},
    {'id': 'mulligan', 'name': 'Mulligan',
     'desc': 'Redraw both cards, free.', 'target': 'none'},
    {'id': 'swap', 'name': 'Swap',
     'desc': "Trade one of your cards for one of an opponent's cards.",
     'target': 'swap'},
    {'id': 'sabotage', 'name': 'Sabotage',
     'desc': 'Force a target opponent to randomly redraw one card.',
     'target': 'opponent'},
    {'id': 'insurance', 'name': 'Insurance',
     'desc': 'Lose 1 fewer chip on round loss (min 0).',
     'target': 'none'},
    {'id': 'reveal_all', 'name': 'Reveal All',
     'desc': 'Everyone shows one card.', 'target': 'none'},
    {'id': 'loaded_die', 'name': 'Loaded Die',
     'desc': 'If you hold an Impostor at showdown, roll 3 dice '
             'instead of 2.', 'target': 'none'},
]

QUIP_KINDS = ['join_table', 'draw', 'stand', 'win', 'lose']


def token_def(token_id):
    for t in TOKEN_POOL:
        if t['id'] == token_id:
            return t
    return None


def build_deck():
    """16 cards: Sand 1-6, Blood 1-6, 2 Sylops, 2 Impostors."""
    deck = []
    for v in range(1, 7):
        deck.append({'type': 'sand', 'value': v})
        deck.append({'type': 'blood', 'value': v})
    deck.append({'type': 'sylop', 'value': None})
    deck.append({'type': 'sylop', 'value': None})
    deck.append({'type': 'impostor', 'value': None})
    deck.append({'type': 'impostor', 'value': None})
    return deck


def _mk_player(name, is_bot, avatar, quotes, tokens):
    return {'name': name, 'is_bot': is_bot, 'avatar': avatar,
            'quotes': quotes or {}, 'chips': START_CHIPS, 'spent': 0,
            'hand': [], 'eliminated': False, 'tokens': list(tokens),
            'token_played': False, 'shield': False, 'insurance': False,
            'loaded_die': False, 'free_draw': False, 'embargoed': False,
            'turns_taken': 0}


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


def _random_tokens():
    return random.sample([t['id'] for t in TOKEN_POOL], 3)


def new_game(roster, stake, tokens):
    """Start a Kessel game. tokens = list of 0-3 token ids (human)."""
    pool = roster if roster else FALLBACK_BOTS
    picks = random.sample(pool, min(BOTS_PER_TABLE, len(pool)))
    players = [_mk_player('You', False, None, None, tokens)]
    for b in picks:
        # no-tokens means no tokens for anyone: 3 each or none.
        bt = [] if not tokens else _random_tokens()
        players.append(_mk_player(b['name'], True, b['avatar'],
                                  b['quotes'], bt))
    s = {'players': players, 'deck': [], 'discards': [],
         'pot': 4 * stake, 'stake': stake, 'round': 0,
         'phase': 'play', 'turn': None, 'pending_discard': None,
         'dice_pick': None, 'dice_queue': [],
         'peeked': set(), 'reveal_all': False,
         'events': [], 'round_results': None, 'game_winner': None,
         'paid': False}
    greeters = [i for i in range(1, len(players))
                if players[i]['quotes'].get('join_table')]
    if greeters and random.random() < 0.85:
        _quip(s, random.choice(greeters), 'join_table')
    start_round(s)
    return s


def _first_to_act(s):
    """First seat of a round: rotates with the round number."""
    n = len(s['players'])
    start = (s['round'] - 1) % n
    for i in range(n):
        seat = (start + i) % n
        if not s['players'][seat]['eliminated']:
            return seat
    return None


def start_round(s):
    """Deal a fresh round (fresh 16-card deck)."""
    s['round'] += 1
    s['phase'] = 'play'
    s['pending_discard'] = None
    s['dice_pick'] = None
    s['dice_queue'] = []
    s['peeked'] = set()
    s['reveal_all'] = False
    s['round_results'] = None
    deck = build_deck()
    random.shuffle(deck)
    s['deck'] = deck
    s['discards'] = []
    for p in s['players']:
        p['hand'] = []  # everyone, incl. eliminated (their stale cards
        # would otherwise double-count against the fresh deck)
        if p['eliminated']:
            continue
        p['turns_taken'] = 0
        p['spent'] = 0
        p['token_played'] = False
        p['shield'] = False
        p['insurance'] = False
        p['free_draw'] = False
        p['embargoed'] = False
        # loaded_die persists until it is used at a showdown
        for _ in range(2):
            c = s['deck'].pop()
            if c is not None:
                p['hand'].append(c)
    _add_event(s, None, 'Round %d. Two cards dealt.' % s['round'])
    s['turn'] = _first_to_act(s)


# ------------------------------------------------------------------ deck

def _ensure_deck(s, n=1):
    """If the deck is short, reshuffle the discard pile into it,
    keeping the top discard card in place."""
    if len(s['deck']) < n and len(s['discards']) > 1:
        top = s['discards'].pop()
        rest = s['discards'][:]
        random.shuffle(rest)
        s['deck'].extend(rest)
        s['discards'] = [top]


def _draw_from_deck(s):
    _ensure_deck(s, 1)
    if not s['deck']:
        return None
    return s['deck'].pop()


# ------------------------------------------------------------------ hands

def hand_rank(hand):
    """Rank a 2-card hand. Lower tuple = better hand.

    Returns (rank, gap, is_pair, is_pure). Pure Sabacc (two Sylops)
    beats everything; a pair beats any non-pair; otherwise the
    smallest gap wins. Pure: (-2, 0). Pair: (-1, pair value).
    No pair: (0, gap).
    """
    if len(hand) != 2:
        return ((9, 99), 99, False, False)
    a, b = hand
    if a['type'] == 'sylop' and b['type'] == 'sylop':
        return ((-2, 0), 0, False, True)
    va = a['value']
    vb = b['value']
    # Sylop copies the other card (resolved on the fly; the engine
    # also stamps the value in at showdown).
    if a['type'] == 'sylop':
        va = vb
    if b['type'] == 'sylop':
        vb = va
    if va is None or vb is None:
        return ((9, 99), 99, False, False)
    if va == vb:
        return ((-1, va), 0, True, False)
    gap = abs(va - vb)
    return ((0, gap), gap, False, False)


def _resolve_sylops(hand):
    """Stamp Sylop values: a Sylop copies the other card's value."""
    if len(hand) != 2:
        return
    a, b = hand
    if a['type'] == 'sylop' and b['type'] == 'sylop':
        return  # Pure Sabacc; values stay None
    if a['type'] == 'sylop':
        a['value'] = b['value']
    if b['type'] == 'sylop':
        b['value'] = a['value']


def _best_die(s, seat, card_idx, dice):
    """Bot helper: which die value gives the best hand? Ties -> lowest."""
    p = s['players'][seat]
    best_d = dice[0]
    best_rank = None
    for d in dice:
        orig = p['hand'][card_idx]['value']
        p['hand'][card_idx]['value'] = d
        rank, _, _, _ = hand_rank(p['hand'])
        p['hand'][card_idx]['value'] = orig
        if best_rank is None or rank < best_rank \
                or (rank == best_rank and d < best_d):
            best_rank = rank
            best_d = d
    return best_d


# ------------------------------------------------------------------ flow

def _next_turn(s):
    """Advance to the next player with turns left, skipping the
    eliminated, consuming embargoed turns. No one left -> showdown."""
    n = len(s['players'])
    for i in range(1, n + 1):
        seat = (s['turn'] + i) % n
        p = s['players'][seat]
        if p['eliminated']:
            continue
        if p['turns_taken'] >= TURNS_PER_ROUND:
            continue
        if p['embargoed']:
            p['embargoed'] = False
            p['turns_taken'] += 1
            _add_event(s, seat, '%s is embargoed and skips a turn.'
                       % p['name'])
            continue
        s['turn'] = seat
        p['token_played'] = False
        return
    _start_showdown(s)


def do_action(s, seat, action, idx=None, source=None):
    """Draw / stand / discard during a turn. Draw leaves the player
    with 3 cards; they must discard before the turn advances."""
    if s['phase'] != 'play':
        raise ValueError('not in a turn')
    if s['turn'] != seat:
        raise ValueError('not your turn')
    p = s['players'][seat]
    if p['eliminated']:
        raise ValueError('you are out')
    if s['pending_discard'] is not None:
        if s['pending_discard'] != seat or action != 'discard':
            raise ValueError('discard a card first')
        if idx is None or not 0 <= idx < len(p['hand']):
            raise ValueError('pick a card to discard')
        card = p['hand'].pop(idx)
        s['discards'].append(card)
        s['pending_discard'] = None
        p['turns_taken'] += 1
        _add_event(s, seat, '%s discards.' % p['name'])
        _next_turn(s)
        return s
    if action == 'stand':
        p['turns_taken'] += 1
        _add_event(s, seat, '%s stands.' % p['name'])
        if p['is_bot']:
            _maybe_quip(s, seat, 'stand', 0.4)
        _next_turn(s)
    elif action == 'draw':
        if source == 'discard':
            if not s['discards']:
                raise ValueError('discard pile is empty')
            card = s['discards'].pop()
        else:
            card = _draw_from_deck(s)
            if card is None:
                raise ValueError('deck is empty')
        if p['free_draw']:
            p['free_draw'] = False
        else:
            if p['chips'] < 1:
                # put the card back where it came from
                if source == 'discard':
                    s['discards'].append(card)
                else:
                    s['deck'].append(card)
                raise ValueError('no chips to draw')
            p['chips'] -= 1
            p['spent'] += 1
        p['hand'].append(card)
        s['pending_discard'] = seat
        _add_event(s, seat, '%s draws a card.' % p['name'])
        if p['is_bot']:
            _maybe_quip(s, seat, 'draw', 0.45)
    else:
        raise ValueError('unknown action')
    return s


def do_token(s, seat, token_id, target=None, own_idx=None,
             target_idx=None):
    """Play a shift token at the start of your turn. One token per
    turn; the turn does NOT advance (you still Draw/Stand after)."""
    if s['phase'] != 'play':
        raise ValueError('not in a turn')
    if s['turn'] != seat:
        raise ValueError('not your turn')
    p = s['players'][seat]
    if p['eliminated']:
        raise ValueError('you are out')
    if s['pending_discard'] is not None:
        raise ValueError('discard a card first')
    if p['token_played']:
        raise ValueError('already played a token this turn')
    if token_id not in p['tokens']:
        raise ValueError('you do not have that token')
    tdef = token_def(token_id)
    if tdef is None:
        raise ValueError('unknown token')
    if tdef['target'] == 'opponent':
        if target is None or not 0 <= target < len(s['players']) \
                or target == seat:
            raise ValueError('pick an opponent')
        if s['players'][target]['eliminated']:
            raise ValueError('that player is out')
    if tdef['target'] == 'swap':
        if own_idx is None or not 0 <= own_idx < len(p['hand']):
            raise ValueError('pick one of your cards')
        if target is None or not 0 <= target < len(s['players']) \
                or target == seat:
            raise ValueError('pick an opponent')
        tp = s['players'][target]
        if tp['eliminated']:
            raise ValueError('that player is out')
        if target_idx is None or not 0 <= target_idx < len(tp['hand']):
            raise ValueError('pick one of their cards')
    p['tokens'].remove(token_id)
    p['token_played'] = True
    if token_id == 'free_draw':
        p['free_draw'] = True
        _add_event(s, seat, '%s plays Free Draw.' % p['name'])
    elif token_id == 'tariff':
        gained = 0
        for j, q in enumerate(s['players']):
            if j == seat or q['eliminated'] or q['shield']:
                continue
            if q['chips'] > 0:
                q['chips'] -= 1
                gained += 1
        p['chips'] += gained
        _add_event(s, seat, '%s plays Tariff (+%d chips).'
                   % (p['name'], gained))
    elif token_id == 'embargo':
        tp = s['players'][target]
        if tp['shield']:
            _add_event(s, seat, '%s embargoes %s, but they are Shielded!'
                       % (p['name'], tp['name']))
        else:
            tp['embargoed'] = True
            _add_event(s, seat, '%s embargoes %s.' % (p['name'], tp['name']))
    elif token_id == 'rebate':
        p['chips'] += 2
        _add_event(s, seat, '%s plays Rebate (+2 chips).' % p['name'])
    elif token_id == 'peek':
        if seat == 0:
            s['peeked'].add(target)
        _add_event(s, seat, "%s peeks at %s's hand."
                   % (p['name'], s['players'][target]['name']))
    elif token_id == 'shield':
        p['shield'] = True
        _add_event(s, seat, '%s plays Shield.' % p['name'])
    elif token_id == 'mulligan':
        s['discards'].extend(p['hand'])
        p['hand'] = []
        for _ in range(2):
            c = _draw_from_deck(s)
            if c is not None:
                p['hand'].append(c)
        _add_event(s, seat, '%s plays Mulligan and redraws.' % p['name'])
    elif token_id == 'swap':
        tp = s['players'][target]
        p['hand'][own_idx], tp['hand'][target_idx] = \
            tp['hand'][target_idx], p['hand'][own_idx]
        _add_event(s, seat, '%s swaps a card with %s.'
                   % (p['name'], tp['name']))
    elif token_id == 'sabotage':
        tp = s['players'][target]
        if tp['shield']:
            _add_event(s, seat, '%s sabotages %s, but they are Shielded!'
                       % (p['name'], tp['name']))
        else:
            if tp['hand']:
                j = random.randrange(len(tp['hand']))
                old = tp['hand'].pop(j)
                s['discards'].append(old)
                c = _draw_from_deck(s)
                if c is not None:
                    tp['hand'].append(c)
            _add_event(s, seat, '%s sabotages %s.'
                       % (p['name'], tp['name']))
    elif token_id == 'insurance':
        p['insurance'] = True
        _add_event(s, seat, '%s plays Insurance.' % p['name'])
    elif token_id == 'reveal_all':
        s['reveal_all'] = True
        _add_event(s, seat, '%s plays Reveal All.' % p['name'])
    elif token_id == 'loaded_die':
        p['loaded_die'] = True
        _add_event(s, seat, '%s plays Loaded Die.' % p['name'])
    _maybe_quip(s, seat, 'token', 0.6)
    return s


# ------------------------------------------------------------------ showdown

def _start_showdown(s):
    """Collect Impostor dice picks, then resolve. Bots resolve
    instantly; the human is waited on via phase 'dice'."""
    queue = []
    for i, p in enumerate(s['players']):
        if p['eliminated']:
            continue
        for j, c in enumerate(p['hand']):
            if c['type'] == 'impostor':
                queue.append((i, j))
    s['dice_queue'] = queue
    if queue:
        s['phase'] = 'dice'
        _process_dice_queue(s)
    else:
        _resolve_showdown(s)


def _process_dice_queue(s):
    """Resolve queued Impostor dice. Bots auto-pick; the human pick
    pauses the queue (phase 'dice' + dice_pick)."""
    while s['dice_queue']:
        seat, card_idx = s['dice_queue'][0]
        p = s['players'][seat]
        n_dice = 3 if p['loaded_die'] else 2
        dice = [random.randint(1, 6) for _ in range(n_dice)]
        if p['is_bot']:
            best = _best_die(s, seat, card_idx, dice)
            _apply_die(s, seat, card_idx, best)
            _maybe_quip(s, seat, 'dice', 0.7)
            if p['loaded_die']:
                p['loaded_die'] = False
            s['dice_queue'].pop(0)
            continue
        s['dice_pick'] = {'seat': seat, 'card_idx': card_idx,
                          'dice': dice}
        return
    s['dice_pick'] = None
    _resolve_showdown(s)


def _apply_die(s, seat, card_idx, value):
    s['players'][seat]['hand'][card_idx]['value'] = value


def do_dice_pick(s, seat, value):
    """Human picks one of the rolled dice for their Impostor."""
    if s['phase'] != 'dice':
        raise ValueError('no dice to pick')
    dp = s.get('dice_pick')
    if not dp or dp['seat'] != seat:
        raise ValueError('not your dice pick')
    if value not in dp['dice']:
        raise ValueError('not one of the rolled dice')
    p = s['players'][seat]
    _apply_die(s, seat, dp['card_idx'], value)
    if p['loaded_die']:
        p['loaded_die'] = False
    s['dice_queue'].pop(0)
    s['dice_pick'] = None
    _process_dice_queue(s)
    return s


def _resolve_showdown(s):
    """Rank hands, settle chips, eliminate, check for a game winner."""
    for p in s['players']:
        if not p['eliminated']:
            _resolve_sylops(p['hand'])
    results = []
    for i, p in enumerate(s['players']):
        if p['eliminated']:
            continue
        rank, gap, is_pair, is_pure = hand_rank(p['hand'])
        results.append({'seat': i, 'rank': rank, 'gap': gap,
                        'is_pair': is_pair, 'is_pure': is_pure})
    results.sort(key=lambda r: r['rank'])
    best_rank = results[0]['rank']
    winner_seats = {r['seat'] for r in results if r['rank'] == best_rank}
    payments = []
    for r in results:
        p = s['players'][r['seat']]
        if r['seat'] in winner_seats:
            if p['spent'] > 0:
                p['chips'] += p['spent']
                payments.append({'seat': r['seat'], 'delta': p['spent'],
                                 'reason': 'regain'})
            p['spent'] = 0
        else:
            pay = 0 if r['is_pair'] else r['gap']
            if p['insurance']:
                pay = max(0, pay - 1)
            pay = min(pay, p['chips'])
            p['chips'] -= pay
            p['spent'] = 0
            if pay > 0:
                payments.append({'seat': r['seat'], 'delta': -pay,
                                 'reason': 'loss'})
    for p in s['players']:
        if not p['eliminated'] and p['chips'] == 0:
            p['eliminated'] = True
            _add_event(s, None, '%s is out of chips and eliminated.'
                       % p['name'])
    for r in results:
        if not s['players'][r['seat']]['is_bot']:
            continue
        if r['seat'] in winner_seats:
            _maybe_quip(s, r['seat'], 'win', 0.7)
        else:
            _maybe_quip(s, r['seat'], 'lose', 0.4)
    alive = [p for p in s['players'] if not p['eliminated']]
    s['round_results'] = {
        'winners': sorted(winner_seats),
        'payments': payments,
        'ranks': [{'seat': r['seat'], 'gap': r['gap'],
                   'is_pair': r['is_pair'], 'is_pure': r['is_pure']}
                  for r in results]}
    s['phase'] = 'reveal'
    s['turn'] = None
    if len(alive) <= 1:
        s['phase'] = 'done'
        if len(alive) == 1:
            s['game_winner'] = s['players'].index(alive[0])
            _add_event(s, s['game_winner'], '%s wins the game!'
                       % alive[0]['name'])
        else:
            s['game_winner'] = None
            _add_event(s, None, 'Everyone is out of chips. Draw.')
    return s


# ------------------------------------------------------------------ turn pump

def step_bot(s):
    """Advance exactly one bot turn (token? then draw/stand). The
    frontend paces these with thinking pauses. Returns True if a bot
    acted. Dice phase is always the human's pick (bots resolve
    instantly inside _process_dice_queue)."""
    if s['phase'] != 'play':
        return False
    seat = s['turn']
    if seat is None or seat == 0:
        return False
    p = s['players'][seat]
    if p['eliminated']:
        return False
    _bot_maybe_token(s, seat)
    action, source = bot_choose(s, seat)
    if action == 'draw':
        try:
            do_action(s, seat, 'draw', source=source)
        except ValueError:
            do_action(s, seat, 'stand')
            return True
        idx = _bot_discard_idx(s, seat)
        do_action(s, seat, 'discard', idx=idx)
    else:
        do_action(s, seat, 'stand')
    return True


# ------------------------------------------------------------------ bot AI

def bot_choose(s, seat):
    """Pick a draw/stand move. Wilds and pairs stand; big gaps draw."""
    p = s['players'][seat]
    hand = p['hand']
    if any(c['type'] in ('sylop', 'impostor') for c in hand):
        return ('stand', None)
    vals = [c['value'] for c in hand]
    if len(vals) != 2 or vals[0] == vals[1]:
        return ('stand', None)
    gap = abs(vals[0] - vals[1])
    if p['chips'] < 1 or gap <= 1:
        return ('stand', None)
    if gap < 4 and random.random() >= 0.6:
        return ('stand', None)
    if s['discards']:
        top = s['discards'][-1]
        if top['type'] in ('sand', 'blood') and top['value'] is not None:
            if top['value'] in vals:
                return ('draw', 'discard')
            if abs(top['value'] - vals[0]) < gap \
                    or abs(top['value'] - vals[1]) < gap:
                return ('draw', 'discard')
    return ('draw', 'deck')


def _bot_discard_idx(s, seat):
    """Discard the card whose removal leaves the best 2-card hand."""
    hand = s['players'][seat]['hand']
    if len(hand) <= 2:
        return 0
    best_idx = 0
    best_rank = None
    for i in range(len(hand)):
        rest = [c for j, c in enumerate(hand) if j != i]
        rank, _, _, _ = hand_rank(rest)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_idx = i
    return best_idx


def _bot_pick_token(s, seat):
    """Pick a token + kwargs, or None. Simple heuristics."""
    p = s['players'][seat]
    toks = set(p['tokens'])
    others = [i for i, q in enumerate(s['players'])
              if i != seat and not q['eliminated']]
    if not others:
        return None
    hand = p['hand']
    has_impostor = any(c['type'] == 'impostor' for c in hand)
    has_wild = any(c['type'] in ('sylop', 'impostor') for c in hand)
    vals = [c['value'] for c in hand if c['value'] is not None]
    gap = abs(vals[0] - vals[1]) if len(vals) == 2 else 99
    leader = max(others, key=lambda i: s['players'][i]['chips'])
    avg = sum(q['chips'] for q in s['players']) / len(s['players'])
    if 'loaded_die' in toks and has_impostor:
        return ('loaded_die', {})
    if 'free_draw' in toks and p['chips'] <= 1:
        return ('free_draw', {})
    if 'rebate' in toks and p['chips'] <= 1:
        return ('rebate', {})
    if 'insurance' in toks and p['chips'] <= 2:
        return ('insurance', {})
    if 'tariff' in toks and s['round'] >= 2 and p['chips'] < avg:
        return ('tariff', {})
    if 'embargo' in toks \
            and s['players'][leader]['chips'] > p['chips'] + 1:
        return ('embargo', {'target': leader})
    if 'mulligan' in toks and not has_wild and gap >= 4:
        return ('mulligan', {})
    if 'swap' in toks and not has_wild and gap >= 4:
        tgt = random.choice(others)
        own_idx = max(range(len(hand)),
                      key=lambda i: (hand[i]['value'] or 0))
        return ('swap', {'own_idx': own_idx, 'target': tgt,
                         'target_idx': random.randrange(2)})
    if 'sabotage' in toks and random.random() < 0.3:
        return ('sabotage', {'target': leader})
    if 'shield' in toks \
            and p['chips'] == max(q['chips'] for q in s['players']) \
            and random.random() < 0.3:
        return ('shield', {})
    if 'peek' in toks and random.random() < 0.2:
        return ('peek', {'target': random.choice(others)})
    if 'reveal_all' in toks and random.random() < 0.15:
        return ('reveal_all', {})
    return None


def _bot_maybe_token(s, seat):
    p = s['players'][seat]
    if not p['tokens'] or p['token_played']:
        return
    if random.random() > 0.35:
        return
    choice = _bot_pick_token(s, seat)
    if choice is None:
        return
    token_id, kwargs = choice
    try:
        do_token(s, seat, token_id, **kwargs)
    except ValueError:
        pass  # token fizzled (e.g. shielded target); bot still acts


def legal_actions(s, seat):
    p = s['players'][seat]
    if s['phase'] != 'play' or s['turn'] != seat or p['eliminated']:
        return []
    if s['pending_discard'] is not None:
        return ['discard'] if s['pending_discard'] == seat else []
    acts = ['stand']
    if p['chips'] >= 1 or p['free_draw']:
        acts.append('draw_deck')
        if s['discards']:
            acts.append('draw_discard')
    return acts


def legal_tokens(s, seat):
    p = s['players'][seat]
    if s['phase'] != 'play' or s['turn'] != seat or p['eliminated']:
        return []
    if s['pending_discard'] is not None or p['token_played']:
        return []
    return list(p['tokens'])


# ------------------------------------------------------------------ selftest

def _play_game(s):
    """Play a full game with bot heuristics for every seat (seat 0
    uses the same heuristics, picking the best die like a good
    player)."""
    guard = 0
    while s['phase'] != 'done' and guard < 5000:
        guard += 1
        if s['phase'] == 'dice':
            dp = s.get('dice_pick')
            if dp:
                best = _best_die(s, dp['seat'], dp['card_idx'], dp['dice'])
                do_dice_pick(s, dp['seat'], best)
            else:
                _process_dice_queue(s)
        elif s['phase'] == 'reveal':
            start_round(s)
        elif s['phase'] == 'play':
            seat = s['turn']
            if seat is None:
                break
            if seat == 0:
                _bot_maybe_token(s, 0)
                action, source = bot_choose(s, 0)
                if action == 'draw':
                    try:
                        do_action(s, 0, 'draw', source=source)
                    except ValueError:
                        do_action(s, 0, 'stand')
                        continue
                    do_action(s, 0, 'discard',
                              idx=_bot_discard_idx(s, 0))
                else:
                    do_action(s, 0, 'stand')
            else:
                step_bot(s)
        else:
            break
    return s


def selftest_games(n=200):
    for g in range(n):
        tokens = random.sample([t['id'] for t in TOKEN_POOL], 3)
        s = new_game([], 100, tokens)
        s = _play_game(s)
        assert s['phase'] == 'done', 'game %d stuck in %s' % (g, s['phase'])
        for p in s['players']:
            assert p['chips'] >= 0, 'game %d: negative chips' % g
        ncards = (len(s['deck']) + len(s['discards'])
                  + sum(len(p['hand']) for p in s['players']))
        assert ncards == 16, 'game %d: cards lost (%d)' % (g, ncards)
        if s['game_winner'] is not None:
            assert not s['players'][s['game_winner']]['eliminated'], \
                'game %d: winner eliminated' % g
    print('kessel selftest OK: %d games, no crashes, chips >= 0, '
          '16 cards accounted for' % n)


def selftest_tokens():
    """Exercise every token once so none of them crashes."""
    for t in TOKEN_POOL:
        s = new_game([], 100, [t['id']])
        # force it to be the human's turn with a playable state
        s['turn'] = 0
        s['players'][0]['token_played'] = False
        s['pending_discard'] = None
        kw = {}
        if t['target'] == 'opponent':
            kw['target'] = 1
        if t['target'] == 'swap':
            kw.update({'own_idx': 0, 'target': 1, 'target_idx': 0})
        do_token(s, 0, t['id'], **kw)
        assert t['id'] not in s['players'][0]['tokens']
    print('kessel token selftest OK: all 12 tokens play cleanly')


def selftest_no_tokens():
    s = new_game([], 50, [])
    assert all(p['tokens'] == [] for p in s['players']), \
        'no-tokens must mean no tokens for anyone'
    s = _play_game(s)
    assert s['phase'] == 'done'
    print('kessel no-token selftest OK: tokenless game completes')


if __name__ == '__main__':
    assert len(build_deck()) == 16
    assert hand_rank([{'type': 'sylop', 'value': None},
                      {'type': 'sylop', 'value': None}])[3] is True
    assert hand_rank([{'type': 'sand', 'value': 1},
                      {'type': 'blood', 'value': 1}])[2] is True
    assert hand_rank([{'type': 'sand', 'value': 6},
                      {'type': 'blood', 'value': 1}])[1] == 5
    selftest_tokens()
    selftest_no_tokens()
    selftest_games()
