//+------------------------------------------------------------------+
//| XAU_H1_DonchianBreakout.mq5                                      |
//| Port of Pine v6 "XAU H1 Donchian Breakout" (TradingKit backtest).|
//| 20-bar channel breakout, 200 EMA filter, 10-bar opposite channel |
//| exit, ATR initial stop and a ratcheting ATR stop.                |
//| Channels use the 20 / 10 bars BEFORE the signal bar, as Pine's   |
//| ta.highest(high, n)[1] does.                                     |
//+------------------------------------------------------------------+
#property copyright "Strategy research"
#property version   "1.00"
#property strict
#include <Trade/Trade.mqh>

input int    InpEntryChannel  = 20;     // Entry channel (bars)
input int    InpExitChannel   = 10;     // Exit channel (bars)
input int    InpTrendEMA      = 200;    // Trend EMA filter
input int    InpAtrPeriod     = 14;     // ATR period
input double InpSlAtrMult     = 2.0;    // Initial stop, ATR multiple
input double InpTrailAtrMult  = 3.0;    // Ratchet stop, ATR multiple
input double InpEquityPercent = 100.0;  // % of equity used as position value (Pine test: 100)
input long   InpMagic         = 26111202;

CTrade   trade;
int      hTrend = INVALID_HANDLE, hAtr = INVALID_HANDLE;
datetime g_lastBar = 0;

int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(50);
   hTrend = iMA(_Symbol, PERIOD_H1, InpTrendEMA, 0, MODE_EMA, PRICE_CLOSE);
   hAtr   = iATR(_Symbol, PERIOD_H1, InpAtrPeriod);
   if(hTrend == INVALID_HANDLE || hAtr == INVALID_HANDLE) return INIT_FAILED;
   if(_Period != PERIOD_H1)
      Print("Warning: this EA is designed for H1. Attach it to an H1 chart.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   IndicatorRelease(hTrend);
   IndicatorRelease(hAtr);
  }

bool NewBar()
  {
   datetime t = iTime(_Symbol, PERIOD_H1, 0);
   if(t == 0 || t == g_lastBar) return false;
   g_lastBar = t;
   return true;
  }

double Val(int handle, int shift)
  {
   double a[];
   ArraySetAsSeries(a, true);
   if(CopyBuffer(handle, 0, shift, 1, a) < 1) return EMPTY_VALUE;
   return a[0];
  }

// Highest high / lowest low of `count` bars starting at `startShift`
double ChannelHigh(int count, int startShift)
  {
   int idx = iHighest(_Symbol, PERIOD_H1, MODE_HIGH, count, startShift);
   return idx < 0 ? EMPTY_VALUE : iHigh(_Symbol, PERIOD_H1, idx);
  }
double ChannelLow(int count, int startShift)
  {
   int idx = iLowest(_Symbol, PERIOD_H1, MODE_LOW, count, startShift);
   return idx < 0 ? EMPTY_VALUE : iLow(_Symbol, PERIOD_H1, idx);
  }

bool GetPosition(long &type, double &sl, ulong &ticket)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      type = PositionGetInteger(POSITION_TYPE);
      sl = PositionGetDouble(POSITION_SL);
      ticket = t;
      return true;
     }
   return false;
  }

double CalcLots(ENUM_ORDER_TYPE ot, double price)
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY) * InpEquityPercent / 100.0;
   double cs   = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(cs <= 0 || price <= 0 || step <= 0) return 0;
   double lots = MathFloor(equity / (price * cs) / step) * step;
   lots = MathMin(lots, vmax);
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   while(lots >= vmin)
     {
      double m = 0;
      if(OrderCalcMargin(ot, _Symbol, lots, price, m) && m <= freeMargin * 0.95)
         return NormalizeDouble(lots, 2);
      lots -= step;
     }
   return 0;
  }

bool OpenTrade(bool isBuy, double sl)
  {
   double price = isBuy ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double lots = CalcLots(isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, price);
   if(lots <= 0) return false;
   double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   if(isBuy  && sl > price - minDist) sl = price - minDist;
   if(!isBuy && sl < price + minDist) sl = price + minDist;
   sl = NormalizeDouble(sl, _Digits);
   return isBuy ? trade.Buy(lots, _Symbol, price, sl, 0.0) : trade.Sell(lots, _Symbol, price, sl, 0.0);
  }

void MoveStop(ulong ticket, double newSl, bool isBuy)
  {
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID), ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   newSl = NormalizeDouble(newSl, _Digits);
   if(isBuy  && newSl > bid - minDist) return;
   if(!isBuy && newSl < ask + minDist) return;
   trade.PositionModify(ticket, newSl, PositionGetDouble(POSITION_TP));
  }

void OnTick()
  {
   if(!NewBar()) return;

   double upper  = ChannelHigh(InpEntryChannel, 2);
   double lower  = ChannelLow(InpEntryChannel, 2);
   double exitUp = ChannelHigh(InpExitChannel, 2);
   double exitDn = ChannelLow(InpExitChannel, 2);
   double flt = Val(hTrend, 1), atr = Val(hAtr, 1);
   if(upper == EMPTY_VALUE || lower == EMPTY_VALUE || exitUp == EMPTY_VALUE || exitDn == EMPTY_VALUE ||
      flt == EMPTY_VALUE || atr == EMPTY_VALUE || atr <= 0) return;
   double c = iClose(_Symbol, PERIOD_H1, 1);

   long type = -1; double sl = 0; ulong ticket = 0;
   bool has = GetPosition(type, sl, ticket);
   bool wasLong  = has && type == POSITION_TYPE_BUY;
   bool wasShort = has && type == POSITION_TYPE_SELL;

   // Entry conditions use the position state at the start of the bar (position_size <= 0 / >= 0)
   bool longEntry  = c > upper && c > flt && !wasLong;
   bool shortEntry = c < lower && c < flt && !wasShort;

   // Channel exits
   if(wasLong  && c < exitDn) { trade.PositionClose(ticket); has = false; }
   if(wasShort && c > exitUp) { trade.PositionClose(ticket); has = false; }

   bool entered = false;
   if(longEntry)
     {
      if(has) { trade.PositionClose(ticket); has = false; }   // reverse a short
      entered = OpenTrade(true, c - atr * InpSlAtrMult);
     }
   else if(shortEntry)
     {
      if(has) { trade.PositionClose(ticket); has = false; }   // reverse a long
      entered = OpenTrade(false, c + atr * InpSlAtrMult);
     }

   // Ratchet the stop (only toward profit) on positions that were not opened this bar
   if(has && !entered && GetPosition(type, sl, ticket))
     {
      if(type == POSITION_TYPE_BUY)
        {
         double ns = c - atr * InpTrailAtrMult;
         if(sl == 0 || ns > sl) MoveStop(ticket, ns, true);
        }
      else
        {
         double ns = c + atr * InpTrailAtrMult;
         if(sl == 0 || ns < sl) MoveStop(ticket, ns, false);
        }
     }
  }
