import pandas as pd
from datetime import date
from jugaad_data.nse import (index_raw, index_pe_raw, index_tri_raw,
                              index_type_list, index_subtype_list, index_name_list)
from jugaad_data.nse import stock_df
df1 = stock_df(symbol="ADANIPOWER", from_date=date(2026,1,1),
            to_date=date(2026,8,31), series="EQ")
df1.to_csv("adanipower_data.csv")
# # OHLC data for an index
# data = index_raw("BANKNIFTY", date(2026, 1, 1), date(2026, 8, 31))
# df = pd.DataFrame(data)
# df.to_csv("banknifty_data.csv")
# # P/E, P/B and Dividend Yield
# pe_data = index_pe_raw("NIFTY 50", date(2026, 1, 1), date(2026, 7, 31))

# # Total Return Index values
# # For standard indices name == index_name; for strategy indices use the short code as name
# tri_data = index_tri_raw("NIFTY 50", "NIFTY 50", date(2026, 1, 1), date(2026, 7, 31))

# Discover available indices (3-level hierarchy)
index_type_list()                                              # ['Equity', 'Fixed Income', 'Multi Asset']
print(index_subtype_list('Equity', 'Historical Index Data'))          # ['Broad Market Indices', 'Sectoral Indices', ...]
print(index_name_list('Broad Market Indices', 'Historical Index Data'))  # ['NIFTY 50', 'NIFTY 100', ...]

# index_group options:
#   'Historical Index Data'
#   'Total returns Index Values '
#   'P/E, P/B & Div.Yield values'