import trade_data
import pandas as pd
import numpy as np
import sys
import os
from time import time
from common_functions import round_up, round_dn, format_float, ts_to_day


def find_precision(vals: np.array, md: int = 10):
    cvals = vals[::len(vals)//100]
    return sorted([len(format_float(round(e, md)).split('.')[1])
                   for e in cvals])[int(len(cvals) * 0.95)]


def to_f(str_val):
    try:
        return float(str_val)
    except:
        return str_val[0] == 'T'


def iterate_trades(symbols: [str], n_days: int):
    base_path = 'historical_data/raw_trades/'
    ts = time() - 60 * 60 * 24 * n_days
    day = ts_to_day(ts)
    end_day = ts_to_day(time())
    while day < end_day:
        print(day)
        day_trades = []
        for s in symbols:
            s_ = s.replace('/', '_')
            filepath = f'{base_path}{s_}/{day}.csv'
            with open(filepath) as f:
                lines = f.readlines()
            header = lines[0].strip().split(',')
            lines = [line.split(',') for line in lines[1:]]
            day_trades += [{**{'symbol': s},
                            **{header[i]: to_f(line[i]) for i in range(len(header))}}
                           for line in lines]
        yield sorted(day_trades, key=lambda x: x['timestamp'])
        ts += 60 * 60 * 24
        day = ts_to_day(ts)


def calc_ema_from_raw_trades(rt: pd.DataFrame, spans: [float]) -> pd.DataFrame:
    '''
    ema span in minutes
    rt timestamps in milliseconds
    '''
    try:
        _ = iter(spans)
    except TypeError:
        spans = [spans]
    df = rt[['price', 'timestamp']].join(pd.Series(rt.index, index=rt.index))
    df.loc[:, 'timestamp'] = df['timestamp'] // 1000 * 1000 # seconds
    seconds = df.groupby('timestamp').last().reindex(
        np.arange(df['timestamp'].iloc[0], df['timestamp'].iloc[-1] + 1, 1000)).fillna(method='ffill')
    emas = pd.DataFrame(
        {'ema_{:.0f}m'.format(span): seconds['price'].ewm(span=(span * 60), adjust=False).mean()
         for span in spans})
    reindexed = emas.reindex(df['timestamp'])
    reindexed.index = rt.index
    return reindexed


def create_df(symbols: [str],
              n_days: int,
              settings: dict,
              no_download: bool = False):
    dfs = []
    for s in symbols:
        print('preparing', s)
        rt = trade_data.fetch_raw_trades(s, n_days=n_days, no_download=no_download)
        print('precision')
        precision = find_precision(rt.price)
        print('emas')
        start_ts = time()
        rt_emas = calc_ema_from_raw_trades(rt, settings['ema_spans_minutes'])
        print('elapsed seconds calc emas', round(time() - start_ts, 2))
        ema_min = rt_emas.min(axis=1)
        ema_max = rt_emas.max(axis=1)
        long_entry = round_dn(ema_min * (1 - settings['entry_spread']), precision)
        shrt_entry = round_up(ema_max * (1 + settings['entry_spread']), precision)
        long_exit = round_up(ema_max, precision)
        shrt_exit = round_dn(ema_min, precision)
        rt.loc[:, 'entry_price'] = long_entry.where(rt.is_buyer_maker, shrt_entry)
        rt.loc[:, 'exit_price'] = long_exit.where(~rt.is_buyer_maker, shrt_exit)
        rt.loc[:, 'entry'] = ((rt.is_buyer_maker & (rt.price < long_entry)) |
                              (~rt.is_buyer_maker & (rt.price > shrt_entry)))
        rt = rt[((rt.is_buyer_maker & (rt.price < shrt_exit)) |
                (~rt.is_buyer_maker & (rt.price > long_exit)))]
        rt.loc[:, 'symbol'] = np.repeat(s, len(rt))
        dfs.append(rt.set_index('timestamp'))
    return pd.concat(dfs, axis=0).sort_index()

def create_df_old(symbols: [str],
              n_days: int,
              ema_spans: [int],
              entry_spread: float,
              no_download: bool = False):
    dfs = []
    for s in symbols:
        print('preparing', s)
        rt = trade_data.fetch_raw_trades(s, n_days=n_days, no_download=no_download)
        precision = find_precision(rt.price)
        rt_emas = calc_ema_from_raw_trades(rt, ema_spans)
        ema_min = (rt_emas.min(axis=1) * (1 - entry_spread)).apply(lambda x: round_dn(x, precision))
        ema_max = (rt_emas.max(axis=1) * (1 + entry_spread)).apply(lambda x: round_up(x, precision))
        buys = rt[(rt.is_buyer_maker & (rt.price < ema_min))]
        b_grouped = buys.groupby('timestamp')
        buys_df = pd.concat([b_grouped.amount.sum(),
                             b_grouped.price.max(),
                             b_grouped.is_buyer_maker.last()], axis=1)
        sels = rt[((~rt.is_buyer_maker) & (rt.price > ema_max))]
        s_grouped = sels.groupby('timestamp')
        sels_df = pd.concat([s_grouped.amount.sum(),
                             s_grouped.price.min(),
                             s_grouped.is_buyer_maker.last()], axis=1)
        df = pd.concat([buys_df, sels_df], axis=0).sort_index()
        df = df.join(pd.Series(np.repeat(s, len(df)), index=df.index, name='symbol'))
        dfs.append(df)
    return pd.concat(dfs, axis=0).sort_index()


def backtest(df: pd.DataFrame, settings: dict):
    symbols = list(df.symbol.unique())
    df_mid = df.iloc[int(len(df) * 0.5):int(len(df) * 0.55)]
    precisions = {s: find_precision(df_mid[df_mid.symbol == s].price.values) for s in symbols}

    ppctminus = 1 - settings['profit_pct']
    ppctplus = 1 + settings['profit_pct']
    s2c = {s: s.split('/')[0] for s in symbols}
    quot = symbols[0].split('/')[1]

    balance = {s2c[s]: 0.0 for s in s2c}
    balance[quot] = settings['start_quot']
    balance_ito_quot = {c: balance[c] for c in balance}

    acc_equity_quot = settings['start_quot']
    acc_debt_quot = 0.0

    entries_list = []
    exits_list = []
    exit_prices_list = []

    long_exit_price = {s: 0.0 for s in symbols}
    shrt_exit_price = {s: 0.0 for s in symbols}

    prev_long_entry_ts = {s: 0 for s in symbols}
    prev_shrt_entry_ts = {s: 0 for s in symbols}

    long_cost = {s: 0.0 for s in symbols}
    long_amount = {s: 0.0 for s in symbols}
    shrt_cost = {s: 0.0 for s in symbols}
    shrt_amount = {s: 0.0 for s in symbols}

    fee = 1 - settings['fee']
    margin = settings['margin'] - 1
    exponent = settings['entry_vol_modifier_exponent']
    max_multiplier = settings['min_exit_cost_multiplier'] - 1
    max_cost_per_symbol_per_hour = settings['account_equity_pct_per_hour'] / len(symbols)
    millis_wait_until_next_long_entry = {s: 0 for s in symbols}
    millis_wait_until_next_shrt_entry = {s: 0 for s in symbols}

    balance_list = []

    cost = min(acc_equity_quot * settings['account_equity_pct_per_trade'],
           settings['min_quot_cost'])

    start_ts, end_ts = df.index[0], df.index[-1]
    ts_range = end_ts - start_ts
    k = 0

    hour_to_millis = 60 * 60 * 1000

    for row in df.itertuples():
        s = row.symbol
        coin = s2c[s]
        default_cost = max(acc_equity_quot * settings['account_equity_pct_per_trade'],
                           settings['min_quot_cost'])
        credit_avbl_quot = max(0.0, acc_equity_quot * margin - acc_debt_quot)
        # bag_size_over_acc_equity = \
        #     sum([abs(v) for v in balance_ito_quot.values()]) / acc_equity_quot

        if row.entry:
            if row.is_buyer_maker:
                if row.Index - prev_long_entry_ts[s] >= millis_wait_until_next_long_entry[s]:
                    cost = default_cost
                    if long_cost[s] > 0.0:
                        cost *= max(
                            1.0,
                            min(max_multiplier, (long_exit_price[s] / row.price) ** exponent)
                        )
                    cost = min(max(balance[quot], credit_avbl_quot), cost)
                    if cost >= settings['min_quot_cost']:
                        amount = cost / row.entry_price
                        balance[quot] -= cost
                        balance[coin] += amount * fee
                        entries_list.append({'symbol': s, 'timestamp': row.Index, 'side': 'buy',
                                             'amount': amount, 'price': row.entry_price,
                                             'cost': cost})
                        long_cost[s] += cost
                        long_amount[s] += amount
                        long_exit_price[s] = round_up((long_cost[s] / long_amount[s]) * ppctplus,
                                                      precisions[s])
                        exit_prices_list.append({'timestamp': row.Index, 'symbol': s,
                                                 'side': 'sell',
                                                 'price': max(long_exit_price[s], row.exit_price)})
                        prev_long_entry_ts[s] = row.Index
                        millis_wait_until_next_long_entry[s] = \
                            1 / (max_cost_per_symbol_per_hour / (default_cost * hour_to_millis))
            else:
                if row.Index - prev_shrt_entry_ts[s] >= millis_wait_until_next_shrt_entry[s]:
                    cost = default_cost
                    if shrt_cost[s] > 0.0:
                        cost *= max(
                            1.0,
                            min(max_multiplier, (row.price / shrt_exit_price[s]) ** exponent)
                        )
                    cost = min(max(balance[quot], credit_avbl_quot), cost)
                    if cost >= settings['min_quot_cost']:
                        amount = cost / row.entry_price
                        balance[coin] -= amount
                        balance[quot] += cost * fee
                        entries_list.append({'symbol': s, 'timestamp': row.Index, 'side': 'sell',
                                             'amount': amount, 'price': row.entry_price,
                                             'cost': cost})
                        shrt_cost[s] += cost
                        shrt_amount[s] += amount
                        shrt_exit_price[s] = round_dn((shrt_cost[s] / shrt_amount[s]) * ppctminus,
                                                      precisions[s])
                        exit_prices_list.append({'timestamp': row.Index, 'symbol': s, 'side': 'buy',
                                                 'price': min(shrt_exit_price[s], row.exit_price)})
                        prev_shrt_entry_ts[s] = row.Index
                        millis_wait_until_next_shrt_entry[s] = \
                            1 / (max_cost_per_symbol_per_hour / (default_cost * hour_to_millis))
        if row.is_buyer_maker:
            exit_price = min(row.exit_price, shrt_exit_price[s])
            if row.price < exit_price and \
                    shrt_cost[s] >= default_cost * settings['min_exit_cost_multiplier']:
                exit_cost = shrt_amount[s] * exit_price
                partial_cost = balance[quot] + credit_avbl_quot
                diff = partial_cost - exit_cost
                if diff >= 0.0:
                    # full exit
                    balance[quot] -= exit_cost
                    balance[coin] += shrt_amount[s] * fee
                    exits_list.append({'symbol': s, 'timestamp': row.Index, 'side': 'buy',
                                       'amount': shrt_amount[s], 'price': exit_price,
                                       'cost': exit_cost})
                    shrt_amount[s], shrt_cost[s], shrt_exit_price[s] = 0.0, 0.0, row.price
                elif partial_cost >= default_cost * settings['min_exit_cost_multiplier']:
                    # partial exit

                    balance[quot] -= partial_cost
                    partial_amount = partial_cost / exit_price
                    balance[coin] += partial_amount * fee
                    exits_list.append({'symbol': s, 'timestamp': row.Index, 'side': 'buy',
                                       'amount': partial_amount, 'price': exit_price,
                                       'cost': partial_cost})
                    shrt_amount[s] -= partial_amount
                    shrt_cost[s] -= partial_cost
                    if shrt_amount[s] <= 0.0 or shrt_cost[s] <= 0.0:
                        shrt_amount[s], shrt_cost[s], shrt_exit_price[s] = 0.0, 0.0, row.price
                    else:
                        shrt_exit_price[s] = shrt_cost[s] / shrt_amount[s]
        else:
            exit_price = max(row.exit_price, long_exit_price[s])
            if row.price > exit_price and \
                    long_cost[s] >= default_cost * settings['min_exit_cost_multiplier']:
                credit_avbl_coin = \
                    max(0.0, acc_equity_quot * margin - acc_debt_quot) / row.price
                partial_amount = balance[coin] + credit_avbl_coin
                diff = partial_amount - long_amount[s]
                if diff >= 0.0:
                    # full exit
                    balance[coin] -= long_amount[s]
                    exit_cost = long_amount[s] * exit_price
                    balance[quot] += exit_cost * fee
                    exits_list.append({'symbol': s, 'timestamp': row.Index, 'side': 'sell',
                                       'amount': long_amount[s], 'price': exit_price,
                                       'cost': exit_cost})
                    long_amount[s], long_cost[s], long_exit_price[s] = 0.0, 0.0, row.price

                elif partial_amount > \
                        default_cost * settings['min_exit_cost_multiplier'] / exit_price:
                    # partial exit
                    balance[coin] -= partial_amount
                    exit_cost = partial_amount * exit_price
                    balance[quot] += exit_cost * fee
                    exits_list.append({'symbol': s, 'timestamp': row.Index, 'side': 'sell',
                                       'amount': long_amount[s], 'price': exit_price,
                                       'cost': exit_cost})
                    long_amount[s] -= partial_amount
                    long_cost[s] -= exit_cost
                    if long_amount[s] <= 0.0 or long_cost[s] <= 0.0:
                        long_amount[s], long_cost[s], long_exit_price[s] = 0.0, 0.0, row.price
                    else:
                        long_exit_price[s] = round_up((long_cost[s] / long_amount[s]) * ppctplus,
                                                 precisions[s])

        acc_equity_quot -= (balance_ito_quot[coin] + balance_ito_quot[quot])
        acc_debt_quot -= -(min(0.0, balance_ito_quot[coin]) + min(0.0, balance_ito_quot[quot]))

        balance_ito_quot[coin] = balance[coin] * row.price
        balance_ito_quot[quot] = balance[quot]

        acc_equity_quot += (balance_ito_quot[coin] + balance_ito_quot[quot])
        acc_debt_quot += -(min(0.0, balance_ito_quot[coin]) + min(0.0, balance_ito_quot[quot]))

        onhand_ito_quot = sum([max(0.0, v) for v in balance_ito_quot.values()])
        margin_level = onhand_ito_quot / acc_debt_quot if acc_debt_quot > 0.0 else 9e9
        if margin_level <= settings['liquidation_margin_level']:
            print('\nliquidation!')
            return balance_list, entries_list, exits_list, exit_prices_list

        k += 1
        if k % 5000 == 0:
            balance_list.append({**balance_ito_quot, **{'acc_equity_quot': acc_equity_quot,
                                                        'acc_debt_quot': acc_debt_quot,
                                                        'onhand_ito_quot': onhand_ito_quot,
                                                        'credit_avbl_quot': credit_avbl_quot,
                                                        'timestamp': row.Index}})
            n_millis = row.Index - start_ts
            n_days = n_millis / 1000 / 60 / 60 / 24
            line = f'\r{(n_millis / ts_range) * 100:.2f}% '
            line += f'n_days {n_days:.2f} '
            line += f'acc equity quot: {acc_equity_quot:.6f}  '
            line += f"avg daily gain: {(acc_equity_quot / settings['start_quot'])**(1/n_days):6f} "
            line += f'cost {default_cost:.8f} margin_level {margin_level:.4f} '
            line += str(sorted([(round(millis_wait_until_next_long_entry[k] / 1000, 2), k)
                                for k in millis_wait_until_next_long_entry]))
            sys.stdout.write(line)
            sys.stdout.flush()

    return balance_list, entries_list, exits_list, exit_prices_list


def main():
    from passivbot import load_settings
    settings = load_settings('default')
    fee = 1 - 0.0675 * 0.01 # vip1

    settings['hours_rolling_small_trade_window'] = 1.0
    settings['account_equity_pct_per_trade'] = 0.0002
    settings['exponent'] = 15


    symbols = [f'{c}/BTC' for c in settings['coins_long']]
    symbols = sorted(symbols)
    n_days = 30 * 13
    #symbols = [s for s in symbols if not any(s.startswith(c) for c in ['VET', 'IOST'])]
    print('loading ohlcvs')
    high_low_means = load_hlms(symbols, n_days, no_download=True)
    print('adding emas')
    df = add_emas(high_low_means, settings['ema_spans_minutes'])


    results = []
    for aepph in list(np.linspace(0.00037, 0.0005, 20).round(6)):
        for mmsd in list(np.linspace(120, 120, 1).round().astype(int)):
            print('testing', aepph, mmsd)
            settings['account_equity_pct_per_hour'] = aepph
            settings['max_memory_span_days'] = mmsd

            balance_list, lentr, sentr, lexit, sexit, lexitpl, sexitpl = backtest(df, settings)
            bldf = pd.DataFrame(balance_list).set_index('timestamp')
            start_equity = bldf.acc_equity_quot.iloc[0]
            end_equity = bldf.acc_equity_quot.iloc[-1]
            n_days = (bldf.index[-1] - bldf.index[0]) / 1000 / 60 / 60 / 24
            avg_daily_gain = (end_equity / start_equity)**(1 / n_days)
            print()
            print(aepph, mmsd, 'average daily gain', round(avg_daily_gain, 8))
            print(aepph, mmsd, '    low water mark', bldf.acc_equity_quot.min())
            print(aepph, mmsd, '   high water mark', bldf.acc_equity_quot.max())
            print(aepph, mmsd, '              mean', bldf.acc_equity_quot.mean())
            print(aepph, mmsd, '               end', bldf.acc_equity_quot.iloc[-1])

            print('\n\n')

            results.append({
                'account_equity_pct_per_hour': aepph,
                'max_memory_span_days': mmsd,
                'average daily gain': avg_daily_gain,
                'low water mark': bldf.acc_equity_quot.min(),
                'high water mark': bldf.acc_equity_quot.max(),
                'mean': bldf.acc_equity_quot.mean(),
                'end': bldf.acc_equity_quot.iloc[-1],
            })
    for r in sorted(results, key=lambda x: x['mean']):
        print(r)


if __name__ == '__main__':
    main()
