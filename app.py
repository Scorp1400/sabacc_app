"""Sabacc parlor v2 — Corellian Spike.

One Flask app, three variants behind a picker (Spike is live; Kessel
and Traditional are stubs). Single-player vs 3 bots picked from the
character roster. One gunicorn worker: the table lives in memory.
init_db() runs at import: gunicorn never runs the __main__ block.

No scores, no pot records of any kind. Bot bankrolls persist across
hands on the table; bust all three bots to clear it.
"""
import os
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from sabacc import kessel, spike, traditional

app = Flask(__name__)

BASE = Path(__file__).parent
DB_PATH = os.environ.get('SABACC_DB', str(BASE / 'sabacc.db'))
CHAR_DIR = BASE / 'characters'

TABLES = ['beskar', 'cantina', 'carbonite', 'circuit_forge', 'crest',
          'crimson_felt', 'darkwood', 'deck_plating', 'glyphstone',
          'hull_panels', 'midnight_felt', 'mustafar', 'neon_club',
          'space_veins']
DEFAULT_TABLE = 'beskar'

VARIANTS = [
    {'id': 'spike',
     'name': 'Corellian Spike',
     'blurb': 'The Solo movie game. Two cards, target zero, spike dice.',
     'ready': True},
    {'id': 'kessel',
     'name': 'Kessel Sabacc',
     'blurb': 'The Outlaws game. Shift tokens, blood and sand.',
     'ready': True},
    {'id': 'traditional',
     'name': 'Traditional Sabacc',
     'blurb': 'The classic. Target 23, shifting cards, Idiot\u2019s Array.',
     'ready': True},
]


# ---------------------------------------------------------------- money
# Two-tier money. The BANK is everything the player owns (SQLite,
# follows them across tables). The STAKE is the chips in front of
# them at the current table (capped buy-in). Bot bankrolls live on
# the table in memory; a server restart recovers the stake from
# SQLite and deals on.

MAX_BUYIN = 300
FRESH_BANK = 1000


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS bankroll (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stake_spike (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stake_trad (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance INTEGER NOT NULL)''')
    c.execute('INSERT OR IGNORE INTO bankroll (id, balance) VALUES (1, ?)',
              (FRESH_BANK,))
    c.execute('INSERT OR IGNORE INTO stake_spike (id, balance) VALUES (1, 0)')
    c.execute('INSERT OR IGNORE INTO stake_trad (id, balance) VALUES (1, 0)')
    # Migrate legacy single 'stake' table if present.
    c.execute("""SELECT name FROM sqlite_master WHERE type='table'
                 AND name='stake'""")
    if c.fetchone():
        c.execute('SELECT balance FROM stake WHERE id = 1')
        row = c.fetchone()
        if row:
            c.execute('UPDATE stake_spike SET balance = ? WHERE id = 1',
                      (row[0],))
        c.execute('DROP TABLE stake')
    conn.commit()
    conn.close()


init_db()


def _get(table):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f'SELECT balance FROM {table} WHERE id = 1')
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


def _set(table, balance):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f'UPDATE {table} SET balance = ? WHERE id = 1', (balance,))
    conn.commit()
    conn.close()


def get_bank():
    return _get('bankroll')


def set_bank(balance):
    _set('bankroll', balance)


def get_stake(variant='spike'):
    return _get(f'stake_{variant}')


def set_stake(balance, variant='spike'):
    _set(f'stake_{variant}', balance)


def buy_in(bank, variant='spike'):
    """Move up to MAX_BUYIN from the bank into a fresh table stake."""
    take = min(MAX_BUYIN, max(0, bank))
    set_bank(bank - take)
    set_stake(take, variant)
    return take


# ---------------------------------------------------------------- game state

_game = None
_table = None
_hand_count = 0


def get_table():
    global _table
    if _table is None:
        roster = spike.load_roster(CHAR_DIR)
        stake = get_stake()
        if stake < spike.ANTE:
            stake = buy_in(get_bank())
        _table = spike.new_table(roster, stake)
    return _table


def get_game():
    global _game
    if _game is None:
        _game = spike.new_hand(get_table(), button=_hand_count % 4)
    return _game


def _after(s):
    """Persist a finished hand's table stake."""
    if s['phase'] == 'done':
        spike.sync_table(s, get_table())
        set_stake(s['players'][0]['bankroll'])


def public_state(s):
    revealed = s['phase'] == 'done'
    table = get_table()
    players = []
    for i, p in enumerate(s['players']):
        players.append({
            'name': p['name'],
            'is_bot': p['is_bot'],
            'avatar': p['avatar'],
            'bankroll': p['bankroll'],
            'busted': p['busted'],
            'folded': p['folded'],
            'bet': p['committed'],
            'hand_count': len(p['hand']),
            'hand': [dict(c) for c in p['hand']] if (i == 0 or revealed)
                    else None,
            'total': spike.hand_total(p['hand']) if (i == 0 or revealed)
                     else None,
        })
    human = s['players'][0]
    to_call = 0
    if s['phase'] == 'betting':
        to_call = max(0, s['bet_level'] - human['committed'])
    return {
        'phase': s['phase'],
        'round': s['round'],
        'pot': s['pot'],
        'players': players,
        'turn': s['turn'],
        'human_turn': (s['phase'] in ('actions', 'betting')
                       and s['turn'] == 0 and not human['folded']
                       and not human['busted']),
        'dice': s['dice'],
        'spike_seq': s['spike_seq'],
        'events': s['events'][-6:],
        'stake': human['bankroll'],
        'bank': get_bank(),
        'bet_level': s['bet_level'],
        'to_call': to_call,
        'legal_actions': spike.legal_actions(s, 0),
        'legal_bets': spike.display_bets(s, 0),
        'winner': s['winner'],
        'human_total': spike.hand_total(human['hand']),
        'table_cleared': spike.table_cleared(table)
                         and s['phase'] in ('done', 'cleared'),
        'human_busted': (human['bankroll'] < spike.ANTE
                         and s['phase'] == 'done'),
    }


# ---------------------------------------------------------------- routes

@app.route('/')
def index():
    return render_template('picker.html', variants=VARIANTS)


@app.route('/play/<variant_id>')
def play(variant_id):
    v = next((x for x in VARIANTS if x['id'] == variant_id), None)
    if not v or not v['ready']:
        return render_template('picker.html', variants=VARIANTS)
    if variant_id == 'kessel':
        return render_template('kessel.html', variant=v, tables=TABLES,
                               default_table=DEFAULT_TABLE)
    if variant_id == 'traditional':
        return render_template('traditional.html', variant=v, tables=TABLES,
                               default_table=DEFAULT_TABLE)
    return render_template('game.html', variant=v, tables=TABLES,
                           default_table=DEFAULT_TABLE)


@app.route('/api/tables')
def api_tables():
    return jsonify(TABLES)


@app.route('/api/state')
def api_state():
    return jsonify(public_state(get_game()))


def _deal(button):
    global _game
    _game = spike.new_hand(get_table(), button=button)
    # bot turns are paced by the frontend via /api/tick
    return _game


@app.route('/api/new', methods=['POST'])
def api_new():
    global _hand_count
    if get_table()['human_bankroll'] < spike.ANTE:
        return jsonify({'error': 'busted'}), 400
    _hand_count += 1
    s = _deal(_hand_count % 4)
    return jsonify(public_state(s))


@app.route('/api/new_table', methods=['POST'])
def api_new_table():
    global _table, _game, _hand_count
    # Cash out the live stake. If a hand is in progress on the current
    # table, the freshest number is in the game state (chips already in
    # the pot are forfeited); otherwise use the table's recorded stake.
    # Never create a game here as a side effect.
    old = 0
    if _table is not None:
        old = _table['human_bankroll']
        g = _game
        if (g is not None and g.get('table') is _table
                and g['phase'] not in ('done', 'cleared', 'busted')):
            old = g['players'][0]['bankroll']
    set_bank(get_bank() + old)  # cash out to the bank...
    stake = buy_in(get_bank())  # ...and buy back in, capped
    roster = spike.load_roster(CHAR_DIR)
    _table = spike.new_table(roster, stake)
    _game = None
    _hand_count += 1
    s = _deal(_hand_count % 4)
    st = public_state(s)
    st['bought_in'] = True
    return jsonify(st)


@app.route('/api/leave_table', methods=['POST'])
def api_leave_table():
    """Cash out the player's table stake to the bank and clear the
    table, without buying back in. The frontend navigates to the
    variant picker."""
    global _table, _game
    old = 0
    if _table is not None:
        old = _table['human_bankroll']
        g = _game
        if (g is not None and g.get('table') is _table
                and g['phase'] not in ('done', 'cleared', 'busted')):
            old = g['players'][0]['bankroll']
    set_bank(get_bank() + old)
    set_stake(0)  # clear persisted stake so re-entry buys in fresh
    _table = None
    _game = None
    return jsonify({'bank': get_bank()})


@app.route('/api/buyin', methods=['POST'])
def api_buyin():
    """Broke at the table: buy back in from the bank."""
    global _hand_count
    table = get_table()
    bank = get_bank()
    if bank < spike.ANTE:
        bank = FRESH_BANK  # the house stakes you
    take = min(MAX_BUYIN, bank)
    set_bank(bank - take)
    table['human_bankroll'] += take
    set_stake(table['human_bankroll'])
    _hand_count += 1
    s = _deal(_hand_count % 4)
    st = public_state(s)
    st['bought_in'] = True
    return jsonify(st)


@app.route('/api/action', methods=['POST'])
def api_action():
    s = get_game()
    data = request.get_json(force=True, silent=True) or {}
    action = data.get('action')
    if (s['phase'] != 'actions' or s['turn'] != 0
            or action not in spike.legal_actions(s, 0)):
        return jsonify({'error': 'not a legal action right now'}), 400
    idx = data.get('idx')
    if action == 'swap':
        if not isinstance(idx, int):
            return jsonify({'error': 'pick a card to swap'}), 400
        if not 0 <= idx < len(s['players'][0]['hand']):
            return jsonify({'error': 'bad card index'}), 400
    else:
        idx = None
    try:
        spike.do_action(s, 0, action, idx)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _after(s)
    return jsonify(public_state(s))


@app.route('/api/bet', methods=['POST'])
def api_bet():
    s = get_game()
    data = request.get_json(force=True, silent=True) or {}
    action = data.get('action')
    if (s['phase'] != 'betting' or s['turn'] != 0
            or action not in spike.legal_bets(s, 0)):
        return jsonify({'error': 'not a legal bet right now'}), 400
    try:
        spike.do_bet(s, 0, action)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _after(s)
    return jsonify(public_state(s))


@app.route('/api/tick', methods=['POST'])
def api_tick():
    """Advance one bot turn. The frontend paces these with thinking
    pauses so the table feels live."""
    s = get_game()
    spike.step_bot(s)
    _after(s)
    return jsonify(public_state(s))


# ---------------------------------------------------------------- kessel

_kessel_game = None


def kessel_public_state(s):
    if s is None:
        return {
            'phase': 'setup', 'bank': get_bank(),
            'token_pool': kessel.TOKEN_POOL,
            'stake_tiers': kessel.STAKE_TIERS,
            'players': [], 'round': 0, 'pot': 0, 'stake': 0,
            'turn': None, 'human_turn': False,
            'pending_discard': False, 'discard_top': None,
            'deck_count': 0, 'dice_pick': None, 'events': [],
            'legal_actions': [], 'legal_tokens': [],
            'round_results': None, 'game_winner': None,
            'game_over': False,
        }
    players = []
    for i, p in enumerate(s['players']):
        if s['phase'] in ('reveal', 'done') or i == 0 \
                or i in s['peeked']:
            hand = [dict(c) for c in p['hand']]
        elif s['reveal_all'] and p['hand']:
            hand = [dict(p['hand'][0])] + [None] * (len(p['hand']) - 1)
        else:
            hand = None
        players.append({
            'name': p['name'], 'is_bot': p['is_bot'],
            'avatar': p['avatar'], 'chips': p['chips'],
            'spent': p['spent'], 'hand': hand,
            'hand_count': len(p['hand']), 'tokens': list(p['tokens']),
            'eliminated': p['eliminated'], 'shield': p['shield'],
            'insurance': p['insurance'], 'embargoed': p['embargoed'],
            'loaded_die': p['loaded_die'],
            'turns_taken': p['turns_taken'],
        })
    return {
        'phase': s['phase'], 'round': s['round'], 'pot': s['pot'],
        'stake': s['stake'], 'players': players, 'turn': s['turn'],
        'human_turn': (s['phase'] == 'play' and s['turn'] == 0
                       and not s['players'][0]['eliminated']),
        'pending_discard': s['pending_discard'] == 0,
        'discard_top': dict(s['discards'][-1]) if s['discards'] else None,
        'deck_count': len(s['deck']), 'dice_pick': s.get('dice_pick'),
        'events': s['events'][-6:], 'bank': get_bank(),
        'legal_actions': kessel.legal_actions(s, 0),
        'legal_tokens': kessel.legal_tokens(s, 0),
        'round_results': s.get('round_results'),
        'game_winner': s.get('game_winner'),
        'game_over': s['phase'] == 'done',
        'token_pool': kessel.TOKEN_POOL,
    }


def _kessel_after(s):
    """Pay the game pot to the human winner, once."""
    if s and s['phase'] == 'done' and s.get('game_winner') == 0 \
            and not s.get('paid'):
        set_bank(get_bank() + s['pot'])
        s['paid'] = True


@app.route('/api/kessel/state')
def api_kessel_state():
    return jsonify(kessel_public_state(_kessel_game))


@app.route('/api/kessel/setup', methods=['POST'])
def api_kessel_setup():
    global _kessel_game
    data = request.get_json(force=True, silent=True) or {}
    stake = data.get('stake')
    tokens = data.get('tokens') or []
    if stake not in kessel.STAKE_TIERS:
        return jsonify({'error': 'pick a stake: 50, 100, or 200'}), 400
    if not isinstance(tokens, list) or len(tokens) > 3:
        return jsonify({'error': 'pick up to 3 tokens'}), 400
    valid = {t['id'] for t in kessel.TOKEN_POOL}
    if any(t not in valid for t in tokens):
        return jsonify({'error': 'unknown token'}), 400
    if len(set(tokens)) != len(tokens):
        return jsonify({'error': 'no duplicate tokens'}), 400
    bank = get_bank()
    if bank < 50:
        bank = FRESH_BANK  # the house stakes you
        set_bank(bank)
    if stake > bank:
        return jsonify({'error': 'not enough credits for that stake'}), 400
    set_bank(bank - stake)
    roster = kessel.load_roster(CHAR_DIR)
    _kessel_game = kessel.new_game(roster, stake, tokens)
    return jsonify(kessel_public_state(_kessel_game))


@app.route('/api/kessel/leave_table', methods=['POST'])
def api_kessel_leave_table():
    """Clear the Kessel game without refunding (stake is forfeited,
    same as abandoning via New Game). The frontend navigates to the
    variant picker."""
    global _kessel_game
    _kessel_game = None
    return jsonify({'bank': get_bank()})


@app.route('/api/kessel/action', methods=['POST'])
def api_kessel_action():
    s = _kessel_game
    if s is None:
        return jsonify({'error': 'no game — set up first'}), 400
    data = request.get_json(force=True, silent=True) or {}
    action = data.get('action')
    if action not in kessel.legal_actions(s, 0):
        return jsonify({'error': 'not a legal action right now'}), 400
    try:
        if action == 'discard':
            idx = data.get('idx')
            if not isinstance(idx, int):
                return jsonify({'error': 'pick a card to discard'}), 400
            kessel.do_action(s, 0, 'discard', idx=idx)
        elif action == 'draw_deck':
            kessel.do_action(s, 0, 'draw', source='deck')
        elif action == 'draw_discard':
            kessel.do_action(s, 0, 'draw', source='discard')
        elif action == 'stand':
            kessel.do_action(s, 0, 'stand')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _kessel_after(s)
    return jsonify(kessel_public_state(s))


@app.route('/api/kessel/token', methods=['POST'])
def api_kessel_token():
    s = _kessel_game
    if s is None:
        return jsonify({'error': 'no game — set up first'}), 400
    data = request.get_json(force=True, silent=True) or {}
    token_id = data.get('token')
    if token_id not in kessel.legal_tokens(s, 0):
        return jsonify({'error': 'not a legal token right now'}), 400
    try:
        kessel.do_token(s, 0, token_id,
                        target=data.get('target'),
                        own_idx=data.get('own_idx'),
                        target_idx=data.get('target_idx'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _kessel_after(s)
    return jsonify(kessel_public_state(s))


@app.route('/api/kessel/dice', methods=['POST'])
def api_kessel_dice():
    s = _kessel_game
    if s is None:
        return jsonify({'error': 'no game — set up first'}), 400
    data = request.get_json(force=True, silent=True) or {}
    die = data.get('die')
    if not isinstance(die, int):
        return jsonify({'error': 'pick a die'}), 400
    try:
        kessel.do_dice_pick(s, 0, die)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _kessel_after(s)
    return jsonify(kessel_public_state(s))


@app.route('/api/kessel/tick', methods=['POST'])
def api_kessel_tick():
    s = _kessel_game
    if s is None:
        return jsonify(kessel_public_state(None))
    kessel.step_bot(s)
    _kessel_after(s)
    return jsonify(kessel_public_state(s))


@app.route('/api/kessel/next', methods=['POST'])
def api_kessel_next():
    s = _kessel_game
    if s is None:
        return jsonify({'error': 'no game — set up first'}), 400
    if s['phase'] != 'reveal':
        return jsonify({'error': 'not at a round break'}), 400
    kessel.start_round(s)
    _kessel_after(s)
    return jsonify(kessel_public_state(s))


@app.route('/api/kessel/new_game', methods=['POST'])
def api_kessel_new_game():
    global _kessel_game
    _kessel_game = None
    return jsonify(kessel_public_state(None))


# ---------------------------------------------------------------- traditional

_trad_table = None
_trad_game = None
_trad_hand_count = 0


def get_trad_table():
    global _trad_table
    if _trad_table is None:
        roster = spike.load_roster(CHAR_DIR)
        stake = get_stake('trad')
        if stake < traditional.ANTE:
            stake = buy_in(get_bank(), 'trad')
        _trad_table = traditional.new_table(roster, stake)
    return _trad_table


def get_trad_game():
    global _trad_game
    if _trad_game is None:
        _trad_game = traditional.new_hand(get_trad_table(),
                                          button=_trad_hand_count % 4)
    return _trad_game


def _trad_after(s):
    """Persist a finished hand's table stake (and jackpot)."""
    if s['phase'] == 'done':
        traditional.sync_table(s, get_trad_table())
        set_stake(s['players'][0]['bankroll'], 'trad')


def _trad_deal(button):
    global _trad_game
    _trad_game = traditional.new_hand(get_trad_table(), button=button)
    # bot turns are paced by the frontend via /api/traditional/tick
    return _trad_game


def traditional_public_state(s):
    table = get_trad_table()
    revealed = s['phase'] == 'done'
    players = []
    for i, p in enumerate(s['players']):
        players.append({
            'name': p['name'],
            'is_bot': p['is_bot'],
            'avatar': p['avatar'],
            'bankroll': p['bankroll'],
            'busted': p['busted'],
            'folded': p['folded'],
            'bet': p['committed'],
            'hand_count': len(p['hand']),
            'hand': [dict(c) for c in p['hand']] if (i == 0 or revealed)
                    else None,
            'total': traditional.hand_total(p['hand'])
                     if (i == 0 or revealed) else None,
        })
    human = s['players'][0]
    to_call = 0
    if s['phase'] == 'betting':
        to_call = max(0, s['bet_level'] - human['committed'])
    return {
        'phase': s['phase'],
        'round': s['round'],
        'pot': s['pot'],
        'sabacc_pot': table['jackpot'],
        'players': players,
        'turn': s['turn'],
        'human_turn': ((s['phase'] in ('actions', 'betting')
                        and s['turn'] == 0 and not human['folded']
                        and not human['busted'])
                       or s['phase'] == 'sylop'),
        'dice': s['dice'],
        'shift_seq': s['shift_seq'],
        'last_shift': s['last_shift'],
        'events': s['events'][-6:],
        'stake': human['bankroll'],
        'bank': get_bank(),
        'bet_level': s['bet_level'],
        'to_call': to_call,
        'legal_actions': traditional.legal_actions(s, 0),
        'legal_bets': traditional.display_bets(s, 0),
        'sylop_pick': s['sylop_pick'],
        'winner': s['winner'],
        'human_total': traditional.hand_total(human['hand']),
        'table_cleared': traditional.table_cleared(table)
                         and s['phase'] in ('done', 'cleared'),
        'human_busted': (human['bankroll'] < traditional.ANTE
                         and s['phase'] == 'done'),
    }


@app.route('/api/traditional/state')
def api_traditional_state():
    return jsonify(traditional_public_state(get_trad_game()))


@app.route('/api/traditional/new', methods=['POST'])
def api_traditional_new():
    global _trad_hand_count
    if get_trad_table()['human_bankroll'] < traditional.ANTE:
        return jsonify({'error': 'busted'}), 400
    _trad_hand_count += 1
    s = _trad_deal(_trad_hand_count % 4)
    return jsonify(traditional_public_state(s))


@app.route('/api/traditional/new_table', methods=['POST'])
def api_traditional_new_table():
    global _trad_table, _trad_game, _trad_hand_count
    # Cash out the live stake. If a hand is in progress on the current
    # table, the freshest number is in the game state (chips already in
    # the pot are forfeited); otherwise use the table's recorded stake.
    # Never create a game here as a side effect.
    old = 0
    if _trad_table is not None:
        old = _trad_table['human_bankroll']
        g = _trad_game
        if (g is not None and g.get('table') is _trad_table
                and g['phase'] not in ('done', 'cleared', 'busted')):
            old = g['players'][0]['bankroll']
    set_bank(get_bank() + old)  # cash out to the bank...
    stake = buy_in(get_bank(), 'trad')  # ...and buy back in, capped
    roster = spike.load_roster(CHAR_DIR)
    _trad_table = traditional.new_table(roster, stake)
    _trad_game = None
    _trad_hand_count += 1
    s = _trad_deal(_trad_hand_count % 4)
    st = traditional_public_state(s)
    st['bought_in'] = True
    return jsonify(st)


@app.route('/api/traditional/leave_table', methods=['POST'])
def api_traditional_leave_table():
    """Cash out the player's table stake to the bank and clear the
    table, without buying back in. The frontend navigates to the
    variant picker."""
    global _trad_table, _trad_game
    old = 0
    if _trad_table is not None:
        old = _trad_table['human_bankroll']
        g = _trad_game
        if (g is not None and g.get('table') is _trad_table
                and g['phase'] not in ('done', 'cleared', 'busted')):
            old = g['players'][0]['bankroll']
    set_bank(get_bank() + old)
    set_stake(0, 'trad')  # clear persisted stake so re-entry buys in fresh
    _trad_table = None
    _trad_game = None
    return jsonify({'bank': get_bank()})


@app.route('/api/traditional/buyin', methods=['POST'])
def api_traditional_buyin():
    """Broke at the table: buy back in from the bank."""
    global _trad_hand_count
    table = get_trad_table()
    bank = get_bank()
    if bank < traditional.ANTE:
        bank = FRESH_BANK  # the house stakes you
    take = min(MAX_BUYIN, bank)
    set_bank(bank - take)
    table['human_bankroll'] += take
    set_stake(table['human_bankroll'], 'trad')
    _trad_hand_count += 1
    s = _trad_deal(_trad_hand_count % 4)
    st = traditional_public_state(s)
    st['bought_in'] = True
    return jsonify(st)


@app.route('/api/traditional/action', methods=['POST'])
def api_traditional_action():
    s = get_trad_game()
    data = request.get_json(force=True, silent=True) or {}
    action = data.get('action')
    if (s['phase'] != 'actions' or s['turn'] != 0
            or action not in traditional.legal_actions(s, 0)):
        return jsonify({'error': 'not a legal action right now'}), 400
    idx = data.get('idx')
    if action == 'swap':
        if not isinstance(idx, int):
            return jsonify({'error': 'pick a card to swap'}), 400
        if not 0 <= idx < len(s['players'][0]['hand']):
            return jsonify({'error': 'bad card index'}), 400
    else:
        idx = None
    try:
        traditional.do_action(s, 0, action, idx)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _trad_after(s)
    return jsonify(traditional_public_state(s))


@app.route('/api/traditional/bet', methods=['POST'])
def api_traditional_bet():
    s = get_trad_game()
    data = request.get_json(force=True, silent=True) or {}
    action = data.get('action')
    if (s['phase'] != 'betting' or s['turn'] != 0
            or action not in traditional.legal_bets(s, 0)):
        return jsonify({'error': 'not a legal bet right now'}), 400
    try:
        traditional.do_bet(s, 0, action)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _trad_after(s)
    return jsonify(traditional_public_state(s))


@app.route('/api/traditional/tick', methods=['POST'])
def api_traditional_tick():
    """Advance one bot turn. The frontend paces these with thinking
    pauses so the table feels live."""
    s = get_trad_game()
    traditional.step_bot(s)
    _trad_after(s)
    return jsonify(traditional_public_state(s))


@app.route('/api/traditional/sylop', methods=['POST'])
def api_traditional_sylop():
    """Human declares their Sylop's value at showdown."""
    s = get_trad_game()
    data = request.get_json(force=True, silent=True) or {}
    value = data.get('value')
    if not isinstance(value, int):
        return jsonify({'error': 'pick a value for your Sylop'}), 400
    try:
        traditional.do_sylop(s, 0, value)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _trad_after(s)
    return jsonify(traditional_public_state(s))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
