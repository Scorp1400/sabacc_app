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

from sabacc import spike

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
     'ready': False},
    {'id': 'traditional',
     'name': 'Traditional Sabacc',
     'blurb': 'The classic. Target 23, shifting cards, Idiot\u2019s Array.',
     'ready': False},
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
    c.execute('''CREATE TABLE IF NOT EXISTS stake (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance INTEGER NOT NULL)''')
    c.execute('INSERT OR IGNORE INTO bankroll (id, balance) VALUES (1, ?)',
              (FRESH_BANK,))
    c.execute('INSERT OR IGNORE INTO stake (id, balance) VALUES (1, 0)')
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


def get_stake():
    return _get('stake')


def set_stake(balance):
    _set('stake', balance)


def buy_in(bank):
    """Move up to MAX_BUYIN from the bank into a fresh table stake."""
    take = min(MAX_BUYIN, max(0, bank))
    set_bank(bank - take)
    set_stake(take)
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


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
