# HFT features & cache
HFT 피처 생성: 1분 캐시 → ffill → 정시 수집. taker_buy_turnover, sum_top LSR 축만 신뢰.
캐시 부르기: CacheHandle.rank_daily(w), load_market(with_volume=True).
