//+------------------------------------------------------------------+
//| XAU_M15_VolBreakout.mq5
//| F7 Williams Volatility Breakout from the Day's Open. O = open of the first M15 bar
//| of the UTC day, PR = prior UTC day's high-low. Long when the close crosses above
//| O + k*PR, short when it crosses below O - k*PR, bars opening 02:00-18:00 UTC Mon-Fri,
//| first trigger of the day only. SL = O, R = |entry-O| (skip if R > 1.0*PR), TP 1.5R.
//| Close at the bar opening 21:00 UTC. Controls: k = 0.3 and k = 0.8.
//| Signals use the last CLOSED bar; orders go in at the next bar open |
//| with a fixed SL and TP. Hours are UTC; server time is converted    |
//| with InpVantageDst (GMT+2/+3) or InpUtcOffsetHours.               |
//+------------------------------------------------------------------+
#property copyright "Strategy research"
#property version   "1.00"
#property strict

input double InpK               = 0.5;       // Breakout distance from the day open, x prior-day range
input int    InpStartMin        = 120;       // Entry window start, UTC minute of day (bar open)
input int    InpEndMin          = 1080;      // Entry window end, UTC minute of day (bar open, inclusive)
input int    InpExitMin         = 1260;      // Close when the closed bar opened at/after this UTC minute (entry day)
input double InpTpR             = 1.5;       // Take profit, R
input double InpMaxRiskPR       = 1.0;       // Skip if R > this * prior-day range
input long   InpMagic           = 26111307;
input double InpMaxSpreadFracR = 0.10;  // Skip if spread is above this share of the risk R

#include <Trade/Trade.mqh>

input double InpRiskPercent    = 1.0;   // Risk per trade, % of equity
input bool   InpCapNoLeverage  = false; // true = cap position value at equity (no leverage); off so risk is a true 1%
input bool   InpVantageDst     = true;  // Server time = GMT+2, +3 during US daylight saving (Vantage)
input int    InpUtcOffsetHours = 0;     // Fixed server-minus-UTC hours, used when InpVantageDst is false

CTrade   trade;
datetime g_lastBar = 0;

// US daylight saving: 2nd Sunday of March to 1st Sunday of November (by server date)
bool UsDst(datetime t)
  {
   MqlDateTime d;
   TimeToStruct(t, d);
   if(d.mon < 3 || d.mon > 11) return false;
   if(d.mon > 3 && d.mon < 11) return true;
   MqlDateTime f;
   TimeToStruct(StringToTime(StringFormat("%04d.%02d.01", d.year, d.mon)), f);
   int firstSun = 1 + (7 - f.day_of_week) % 7;
   if(d.mon == 3) return d.day >= firstSun + 7;
   return d.day < firstSun;
  }

int OffsetHours(datetime t)
  {
   return InpVantageDst ? 2 + (UsDst(t) ? 1 : 0) : InpUtcOffsetHours;
  }

bool NewBar()
  {
   datetime t = iTime(_Symbol, PERIOD_M15, 0);
   if(t == 0 || t == g_lastBar) return false;
   g_lastBar = t;
   return true;
  }

long UtcSec(datetime t)  { return (long)t - (long)OffsetHours(t) * 3600; }
int  HourUtc(datetime t) { return (int)((UtcSec(t) / 3600) % 24); }
long DayUtc(datetime t)  { return UtcSec(t) / 86400; }
int  DowUtc(datetime t)  { return (int)((DayUtc(t) + 4) % 7); }   // 0 = Sunday

double BufVal(int handle, int buffer, int shift)
  {
   double a[];
   ArraySetAsSeries(a, true);
   if(CopyBuffer(handle, buffer, shift, 1, a) < 1) return EMPTY_VALUE;
   return a[0];
  }

bool GetPosition(long &type, ulong &ticket, datetime &openTime)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      type = PositionGetInteger(POSITION_TYPE);
      openTime = (datetime)PositionGetInteger(POSITION_TIME);
      ticket = t;
      return true;
     }
   return false;
  }

// Lots so that a stop-out loses InpRiskPercent of equity. Returns 0 (skip) if the minimum lot is too big.
double CalcLots(ENUM_ORDER_TYPE ot, double price, double slDist)
  {
   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   double cs       = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double step     = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin     = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax     = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(tickSize <= 0 || tickVal <= 0 || slDist <= 0 || step <= 0 || price <= 0) return 0;
   double lots = equity * InpRiskPercent / 100.0 / (slDist / tickSize * tickVal);
   if(InpCapNoLeverage && cs > 0) lots = MathMin(lots, equity / (price * cs));
   lots = MathFloor(lots / step) * step;
   lots = MathMin(lots, vmax);
   if(lots < vmin) return 0;
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

// Market order with a fixed SL and TP set at entry (levels come from the signal bar)
bool OpenTrade(bool isBuy, double sl, double tp)
  {
   double price = isBuy ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   if(isBuy)
     {
      if(sl > price - minDist) sl = price - minDist;
      if(tp > 0 && tp < price + minDist) tp = price + minDist;
     }
   else
     {
      if(sl < price + minDist) sl = price + minDist;
      if(tp > 0 && tp > price - minDist) tp = price - minDist;
     }
   sl = NormalizeDouble(sl, _Digits);
   tp = NormalizeDouble(tp, _Digits);
   if(InpMaxSpreadFracR > 0 && (SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID)) > InpMaxSpreadFracR * MathAbs(price - sl)) return false;
   double lots = CalcLots(isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, price, MathAbs(price - sl));
   if(lots <= 0) return false;
   return isBuy ? trade.Buy(lots, _Symbol, price, sl, tp) : trade.Sell(lots, _Symbol, price, sl, tp);
  }

int HeldBars(datetime openTime)
  {
   int s = iBarShift(_Symbol, PERIOD_M15, openTime, false);
   return s < 0 ? 0 : s;
  }


datetime g_exitTime = 0;
bool     g_hadPos = false;
double Hi(int s) { return iHigh(_Symbol, PERIOD_M15, s); }
double Lo(int s) { return iLow(_Symbol, PERIOD_M15, s); }
double Op(int s) { return iOpen(_Symbol, PERIOD_M15, s); }
double Cl(int s) { return iClose(_Symbol, PERIOD_M15, s); }
bool Cooling(int bars) { return g_exitTime != 0 && iBarShift(_Symbol, PERIOD_M15, g_exitTime, false) < bars; }
void TrackExit(bool has) { if(g_hadPos && !has) g_exitTime = iTime(_Symbol, PERIOD_M15, 0); g_hadPos = has; }
int  MinOfDay(datetime t) { return (int)((UtcSec(t) / 60) % 1440); }

long   g_day = -1;
bool   g_dayOk = false;
bool   g_triggered = false;
double g_O = 0, g_PR = 0;

int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(50);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { }

long BarDay(int s) { datetime t = iTime(_Symbol, PERIOD_M15, s); return t == 0 ? -1 : DayUtc(t); }

// Day open O (first bar of 'day') and prior-day range PR (previous UTC day that has bars)
bool DayLevels(long day, double &O, double &PR)
  {
   int i = 1, maxScan = 2000;
   O = 0;
   while(i < maxScan && BarDay(i) == day) { O = Op(i); i++; }
   if(O <= 0 || i >= maxScan) return false;
   // skip weekend UTC days (e.g. the Sunday-evening open bars), so Monday uses Friday's range
   while(i < maxScan && BarDay(i) >= 0 && ((BarDay(i) + 4) % 7 == 0 || (BarDay(i) + 4) % 7 == 6)) i++;
   long pd = BarDay(i);
   if(pd < 0 || i >= maxScan) return false;
   double hh = -DBL_MAX, ll = DBL_MAX;
   while(i < maxScan && BarDay(i) == pd)
     {
      double h = Hi(i), l = Lo(i);
      if(h <= 0 || l <= 0) return false;
      hh = MathMax(hh, h); ll = MathMin(ll, l);
      i++;
     }
   if(i >= maxScan || hh <= ll) return false;   // incomplete prior day
   PR = hh - ll;
   return true;
  }

void OnTick()
  {
   if(!NewBar()) return;
   datetime t1 = iTime(_Symbol, PERIOD_M15, 1), t0 = iTime(_Symbol, PERIOD_M15, 0);
   if(t1 == 0 || t0 == 0) return;
   long type = -1; ulong tk = 0; datetime ot = 0;
   bool has = GetPosition(type, tk, ot);
   TrackExit(has);
   if(has)
     {
      long eday = DayUtc(ot);
      bool timeUp = (DayUtc(t1) == eday && MinOfDay(t1) >= InpExitMin) || DayUtc(t0) > eday;
      if(timeUp) trade.PositionClose(tk);
      return;                                                   // one position at a time
     }

   long day = DayUtc(t1);
   if(day != g_day)
     {
      g_day = day; g_triggered = false;
      g_dayOk = DayLevels(day, g_O, g_PR);
     }
   if(!g_dayOk || g_triggered) return;
   int dow = DowUtc(t1), mod = MinOfDay(t1);
   if(dow < 1 || dow > 5 || mod < InpStartMin || mod > InpEndMin) return;
   datetime t2 = iTime(_Symbol, PERIOD_M15, 2);
   if(t2 == 0 || DayUtc(t2) != day) return;                     // both bars in the current UTC day

   double c1 = Cl(1), c2 = Cl(2);
   if(c1 <= 0 || c2 <= 0) return;
   double up = g_O + InpK * g_PR, dn = g_O - InpK * g_PR;
   bool sigL = c1 > up && c2 <= up;
   bool sigS = c1 < dn && c2 >= dn;
   if(!sigL && !sigS) return;
   g_triggered = true;                                          // first trigger only, even if skipped below

   double R = MathAbs(c1 - g_O);
   if(R <= 0 || R > InpMaxRiskPR * g_PR) return;
   bool ok = sigL ? OpenTrade(true,  g_O, c1 + InpTpR * R)
                  : OpenTrade(false, g_O, c1 - InpTpR * R);
   if(ok) g_hadPos = true;
  }
