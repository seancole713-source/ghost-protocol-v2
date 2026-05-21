#!/usr/bin/env python3
"""
🏛️ GHOST STOCK ENGINE - Stock-Specific Prediction Model
========================================================

Why stocks need a different model than crypto:
- Stocks move 2-3x slower than crypto
- Stocks are affected by market hours, earnings, Fed
- RSI 30 (crypto oversold) rarely happens in stocks
- 48h horizon is too long for stocks (24h better)
- 6% target is unrealistic (2% is achievable)

This engine applies the Ghost blueprint that WORKS for crypto,
but with stock-tuned parameters:

CRYPTO ENGINE          | STOCK ENGINE
-----------------------|--------------------
48h horizon            | 24h horizon
6% target              | 2% target
RSI 30/70              | RSI 35/65
3 confirmations        | 4 confirmations
VIX < 25               | VIX < 20
BTC trend gate         | SPY regime gate
24/7 trading           | Market hours only
No earnings            | Earnings blackout

Target: 40-50% win rate (up from 4.5%)

PRE-MARKET FIX (Feb 24, 2026):
  At 8 AM CT (9 AM ET), the stock market doesn't open until 8:30 CT / 9:30 ET.
  Yesterday's close is the FRESHEST data available — there is literally nothing
  newer to wait for. Daily bars from Polygon/yfinance are perfectly valid.
  The ensemble predictor degrades pre-market because the feature orchestrator
  can't get real-time price/volume, but the stock engine's own daily-bar
  indicators (RSI, MACD, Bollinger, volume ratio, ATR) are fully valid.
  This fix makes the engine pre-market aware so stocks appear in 8 AM cards.
"""

import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field

# Pattern tracking for accuracy measurement
from core.pattern_tracker import record_pattern_detection

# Internal imports - lazy loaded to avoid circular imports
_wolf_app_loaded = False

LOGGER = logging.getLogger("ghost.stock_engine")

# ============================================================================
# TIMEZONE + PRE-MARKET DETECTION
# ============================================================================

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from pytz import timezone as ZoneInfo

_ET = ZoneInfo("America/New_York")

# Pre-market staleness: how old stock data can be before it's "stale".
# Default 18h covers the overnight gap (yesterday 3 PM CT close → today 8 AM CT card).
# On weekends this covers Fri close → Mon 8 AM (≈65h), but the engine already
# blocks weekends via SPY/VIX gates and economic_calendar, so it doesn't matter.
STOCK_DATA_STALENESS_H = float(os.getenv("STOCK_DATA_STALENESS_H", "18"))


def _is_premarket() -> bool:
    """
    True when the US stock market has NOT yet opened today.

    Pre-market = weekday AND current ET time < 9:30 AM ET.
    Returns False on weekends (no card fires on weekends anyway).

    At 8 AM CT = 9 AM ET this returns True, which is exactly when the
    daily TOP 10 card fires. The market opens at 9:30 AM ET (8:30 AM CT),
    so yesterday's close is the freshest data available.
    """
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    time_decimal = now_et.hour + now_et.minute / 60.0
    return time_decimal < 9.5  # Before 9:30 AM ET

# ============================================================================
# STOCK ENGINE CONFIGURATION
# ============================================================================

@dataclass
class StockConfig:
    """Stock-specific prediction parameters (tuned for slower-moving assets)"""
    
    # Prediction horizon (vs 48h for crypto)
    horizon_hours: int = 24
    
    # Target move percentage (vs 6% for crypto)
    target_pct: float = 2.0
    
    # RSI thresholds (RELAXED from 35/65 to 40/60 for better signal generation)
    # Stocks rarely hit 35/65 - we were getting too many HOLDs
    rsi_oversold: float = 40.0
    rsi_overbought: float = 60.0
    
    # Confirmation requirements (reduced from 4 to 3 to allow more predictions)
    min_confirmations: int = 3
    
    # VIX threshold (relaxed from 20 to 22 - 20 was too strict)
    vix_max: float = 22.0
    
    # SPY regime requirement
    require_spy_bull: bool = True
    spy_ma_period: int = 20
    
    # Market hours
    market_hours_only: bool = True
    
    # Earnings blackout
    earnings_blackout_days: int = 7
    
    # Position sizing
    max_position_pct: float = 5.0  # Max 5% of portfolio per stock
    
    # Stop loss / Take profit
    stop_loss_pct: float = 1.0  # Tighter than crypto
    take_profit_pct: float = 2.5  # Smaller targets
    
    # Multi-timeframe requirements
    require_mtf_alignment: bool = True
    mtf_min_agree: int = 2  # At least 2 of 3 timeframes
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "horizon_hours": self.horizon_hours,
            "target_pct": self.target_pct,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "min_confirmations": self.min_confirmations,
            "vix_max": self.vix_max,
            "require_spy_bull": self.require_spy_bull,
            "market_hours_only": self.market_hours_only,
            "earnings_blackout_days": self.earnings_blackout_days,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "require_mtf_alignment": self.require_mtf_alignment,
        }


# Default configuration
STOCK_CONFIG = StockConfig()

# Stock whitelist (high-liquidity, predictable stocks)
STOCK_WHITELIST = {
    # Tech giants (most predictable)
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
    
    # Finance
    "JPM", "BAC", "GS", "MS",
    
    # Consumer
    "TSLA", "DIS", "NKE", "SBUX",
    
    # Healthcare
    "JNJ", "PFE", "UNH",
    
    # Energy
    "XOM", "CVX",
    
    # Industrial
    "CAT", "BA", "GE",
}


@dataclass
class StockPrediction:
    """Stock prediction result"""
    symbol: str
    direction: str  # "UP", "DOWN", "HOLD"
    confidence: float
    entry_price: float
    target_price: float
    stop_loss: float
    horizon_hours: int
    confirmations: int
    gates_passed: List[str]
    gates_failed: List[str]
    reasons: List[str]
    expected_move_pct: float = 0.0  # Predicted magnitude (e.g. 2.5 = +2.5%)
    atr_pct: float = 0.0  # Average True Range as % of price
    data_quality: float = 1.0  # 0.0 = all defaults, 1.0 = all real data
    explanation: str = ""  # Human-readable prediction reasoning
    position_size_usd: float = 0  # Recommended position size in dollars
    shares: int = 0  # Recommended number of shares
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def is_actionable(self) -> bool:
        """True if prediction should generate a signal"""
        return (
            self.direction in ("UP", "DOWN") and
            self.confidence >= 0.6 and
            len(self.gates_failed) == 0 and
            self.confirmations >= STOCK_CONFIG.min_confirmations
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "entry_price": round(self.entry_price, 2),
            "target_price": round(self.target_price, 2),
            "stop_loss": round(self.stop_loss, 2),
            "expected_move_pct": round(self.expected_move_pct, 2),
            "atr_pct": round(self.atr_pct, 2),
            "data_quality": round(self.data_quality, 2),
            "horizon_hours": self.horizon_hours,
            "confirmations": self.confirmations,
            "min_confirmations": STOCK_CONFIG.min_confirmations,
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
            "reasons": self.reasons,
            "is_actionable": self.is_actionable,
            "timestamp": self.timestamp.isoformat(),
        }


class StockEngine:
    """
    Stock-specific prediction engine.
    
    Uses the same ensemble approach as crypto (LSTM + XGBoost + Transformer)
    but with stock-tuned parameters and additional gates.
    """
    
    def __init__(self, config: StockConfig = None):
        self.config = config or STOCK_CONFIG
        self._initialized = False
        self._vix_cache: Tuple[float, float] = (0, 0)  # (value, timestamp)
        self._spy_cache: Tuple[Dict, float] = ({}, 0)  # (data, timestamp)
    
    async def initialize(self):
        """Initialize the engine (load models, etc.)"""
        if self._initialized:
            return
        
        LOGGER.info("🏛️ Initializing Stock Engine...")
        
        # Import gate modules
        try:
            from core.stock_gates import run_all_stock_gates
            from core.sector_momentum import sector_momentum_gate, analyze_sector_momentum
            from core.economic_calendar import economic_calendar_gate
            self._gates_available = True
            LOGGER.info("✅ Stock gates loaded")
        except ImportError as e:
            LOGGER.warning(f"⚠️ Stock gates not fully available: {e}")
            self._gates_available = False
        
        self._initialized = True
        LOGGER.info("🏛️ Stock Engine initialized with config:")
        LOGGER.info(f"   Horizon: {self.config.horizon_hours}h")
        LOGGER.info(f"   Target: {self.config.target_pct}%")
        LOGGER.info(f"   RSI: {self.config.rsi_oversold}/{self.config.rsi_overbought}")
        LOGGER.info(f"   Min confirmations: {self.config.min_confirmations}")
    
    def _load_wolf_app(self):
        """Lazy load wolf_app to avoid circular imports"""
        global _wolf_app_loaded
        if not _wolf_app_loaded:
            try:
                import wolf_app
                self._wolf_app = wolf_app
                _wolf_app_loaded = True
            except ImportError as e:
                LOGGER.error(f"Failed to import wolf_app: {e}")
                self._wolf_app = None
    
    async def _get_vix(self) -> Optional[float]:
        """Get current VIX level (cached for 5 min)"""
        now = time.time()
        if now - self._vix_cache[1] < 300:  # 5 min cache
            return self._vix_cache[0]
        
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="1d")
            if not hist.empty:
                value = hist['Close'].iloc[-1]
                self._vix_cache = (value, now)
                return value
        except Exception as e:
            LOGGER.warning(f"VIX fetch failed: {e}")
        
        return self._vix_cache[0] if self._vix_cache[0] > 0 else None
    
    async def _get_spy_regime(self) -> Tuple[bool, float]:
        """
        Check if SPY is above 20-day MA (bull market).
        Uses yfinance with Polygon fallback.
        
        Returns: (is_bullish, pct_vs_ma)
        """
        now = time.time()
        if now - self._spy_cache[1] < 300:  # 5 min cache
            data = self._spy_cache[0]
            return data.get("bullish", True), data.get("pct_vs_ma", 0)
        
        hist_closes = []
        
        # Try yfinance first
        try:
            import yfinance as yf
            spy = yf.Ticker("SPY")
            hist = spy.history(period="30d")
            
            if len(hist) >= 20:
                hist_closes = hist['Close'].tolist()
        except Exception as e:
            LOGGER.debug(f"SPY yfinance failed: {e}")
        
        # Polygon fallback if yfinance failed
        if len(hist_closes) < 20:
            try:
                import os, httpx
                from datetime import datetime as _dt, timedelta
                api_key = os.getenv("POLYGON_API_KEY", "")
                if api_key:
                    end = _dt.utcnow().strftime("%Y-%m-%d")
                    start = (_dt.utcnow() - timedelta(days=45)).strftime("%Y-%m-%d")
                    url = (
                        f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/"
                        f"{start}/{end}?adjusted=true&sort=asc&apiKey={api_key}"
                    )
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            bars = resp.json().get("results", [])
                            if len(bars) >= 20:
                                hist_closes = [b["c"] for b in bars]
                                LOGGER.info(f"✅ SPY regime: Polygon fallback ({len(bars)} bars)")
            except Exception as e:
                LOGGER.debug(f"SPY Polygon fallback failed: {e}")
        
        if len(hist_closes) >= 20:
            current = hist_closes[-1]
            ma20 = sum(hist_closes[-20:]) / 20
            pct_vs_ma = ((current - ma20) / ma20) * 100
            bullish = current > ma20
            
            self._spy_cache = ({"bullish": bullish, "pct_vs_ma": pct_vs_ma}, now)
            return bullish, pct_vs_ma
        
        # Default: UNKNOWN, not bullish
        LOGGER.warning("SPY regime unknown — all data sources failed. Defaulting to neutral.")
        return False, 0
    
    async def _get_technical_indicators(self, symbol: str) -> Dict[str, Any]:
        """
        Get technical indicators for stock.
        Uses Polygon API with yfinance fallback.
        """
        self._load_wolf_app()
        
        indicators = {
            "rsi_14": None,
            "macd_histogram": None,
            "bb_lower": None,
            "bb_upper": None,
            "ema_20": None,
            "current_price": None,
            "volume_ratio": None,
        }
        
        try:
            import pandas as pd
            import os
            import httpx
            from datetime import datetime, timedelta
            
            hist = None
            
            # Try Polygon first (more reliable than yfinance)
            polygon_key = os.getenv("POLYGON_API_KEY")
            if polygon_key:
                try:
                    end = datetime.now().strftime("%Y-%m-%d")
                    start = (datetime.now() - timedelta(days=35)).strftime("%Y-%m-%d")
                    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}?adjusted=true&apiKey={polygon_key}"
                    
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("results"):
                                results = data["results"]
                                hist = pd.DataFrame(results)
                                hist['Close'] = hist['c']
                                hist['Volume'] = hist['v']
                                LOGGER.info(f"[STOCK-ENGINE] Got {len(hist)} bars from Polygon for {symbol}")
                except Exception as e:
                    LOGGER.warning(f"[STOCK-ENGINE] Polygon failed for {symbol}: {e}")
            
            # Fallback to yfinance
            if hist is None or hist.empty:
                try:
                    import yfinance as yf
                    ticker = yf.Ticker(symbol)
                    hist = ticker.history(period="30d")
                    if not hist.empty:
                        LOGGER.info(f"[STOCK-ENGINE] Got {len(hist)} bars from yfinance for {symbol}")
                except Exception as e:
                    LOGGER.warning(f"[STOCK-ENGINE] yfinance failed for {symbol}: {e}")
            
            if hist is None or hist.empty:
                return indicators
            
            close = hist['Close']
            volume = hist['Volume']
            
            # Current price
            indicators["current_price"] = float(close.iloc[-1])
            
            # RSI 14
            delta = close.diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            indicators["rsi_14"] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
            
            # EMA 12, 26 for MACD
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            macd = ema12 - ema26
            signal = macd.ewm(span=9).mean()
            histogram = macd - signal
            indicators["macd_histogram"] = float(histogram.iloc[-1]) if not pd.isna(histogram.iloc[-1]) else None
            
            # EMA 20
            ema20 = close.ewm(span=20).mean()
            indicators["ema_20"] = float(ema20.iloc[-1])
            
            # Bollinger Bands
            sma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            bb_upper = sma20 + (std20 * 2)
            bb_lower = sma20 - (std20 * 2)
            indicators["bb_upper"] = float(bb_upper.iloc[-1])
            indicators["bb_lower"] = float(bb_lower.iloc[-1])
            
            # Volume ratio (current vs 20d avg)
            avg_volume = volume.rolling(20).mean().iloc[-1]
            current_volume = volume.iloc[-1]
            indicators["volume_ratio"] = float(current_volume / avg_volume) if avg_volume > 0 else 1.0
            
            # ================================================================
            # MAGNITUDE ESTIMATION: ATR + Historical Volatility
            # Used to predict HOW MUCH a stock will move, not just direction
            # ================================================================
            try:
                high = hist['High'] if 'High' in hist.columns else hist.get('h', close)
                low = hist['Low'] if 'Low' in hist.columns else hist.get('l', close)
                
                # ATR (Average True Range) - 14 period
                tr1 = high - low
                tr2 = abs(high - close.shift())
                tr3 = abs(low - close.shift())
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                atr_14 = tr.rolling(14).mean().iloc[-1]
                current_price = float(close.iloc[-1])
                atr_pct = (atr_14 / current_price) * 100 if current_price > 0 else 0
                indicators["atr_14"] = float(atr_14) if not pd.isna(atr_14) else 0
                indicators["atr_pct"] = float(atr_pct) if not pd.isna(atr_pct) else 0
                
                # Historical daily volatility (std of daily returns)
                daily_returns = close.pct_change().dropna()
                daily_vol = float(daily_returns.std()) * 100  # as percentage
                indicators["daily_volatility_pct"] = daily_vol if not pd.isna(daily_vol) else 1.5
                
                # Recent momentum (5-day return)
                if len(close) >= 6:
                    recent_return = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
                    indicators["recent_5d_return_pct"] = recent_return
                
            except Exception as e:
                LOGGER.debug(f"Magnitude indicators failed for {symbol}: {e}")
                indicators["atr_14"] = 0
                indicators["atr_pct"] = 0
                indicators["daily_volatility_pct"] = 1.5
            
        except Exception as e:
            LOGGER.warning(f"Technical indicators failed for {symbol}: {e}")
        
        # ================================================================
        # DATA QUALITY SCORE: Track how many indicators are REAL vs DEFAULT
        # A prediction with 2/7 real indicators is garbage - we need to know
        # ================================================================
        real_indicators = 0
        total_indicators = 7  # RSI, MACD, EMA20, BB, Volume, ATR, Price
        
        if indicators.get("current_price", 0) > 0:
            real_indicators += 1
        if indicators.get("rsi_14") is not None:  # None means unavailable
            real_indicators += 1
        if indicators.get("macd_histogram") is not None:  # None means unavailable
            real_indicators += 1
        if indicators.get("ema_20", 0) > 0:
            real_indicators += 1
        if indicators.get("bb_lower", 0) > 0 and indicators.get("bb_upper", 0) > 0:
            real_indicators += 1
        if indicators.get("volume_ratio", 1.0) != 1.0:  # 1.0 is the default
            real_indicators += 1
        if indicators.get("atr_pct", 0) > 0:
            real_indicators += 1
        
        indicators["data_quality_score"] = real_indicators / total_indicators
        indicators["data_quality_real"] = real_indicators
        indicators["data_quality_total"] = total_indicators
        
        if real_indicators < 4:
            LOGGER.warning(
                f"⚠️ [{symbol}] LOW DATA QUALITY: only {real_indicators}/{total_indicators} "
                f"indicators are real (rest are defaults). Prediction reliability is degraded."
            )
        
        return indicators
    
    async def _count_confirmations(
        self,
        symbol: str,
        direction: str,
        indicators: Dict[str, Any],
        vix: float,
        spy_bullish: bool
    ) -> Tuple[int, List[str]]:
        """
        Count bullish/bearish confirmations using stock-tuned thresholds.
        """
        confirmations = 0
        reasons = []
        
        price = indicators.get("current_price", 0)
        rsi = indicators.get("rsi_14")  # None if unavailable
        macd = indicators.get("macd_histogram")  # None if unavailable
        bb_lower = indicators.get("bb_lower", 0)
        bb_upper = indicators.get("bb_upper", 0)
        ema20 = indicators.get("ema_20", 0)
        volume_ratio = indicators.get("volume_ratio", 1.0)
        
        if direction == "UP":
            # 1. RSI oversold (< 35 for stocks)
            if rsi and rsi < self.config.rsi_oversold:
                confirmations += 1
                reasons.append(f"RSI oversold ({rsi:.0f} < {self.config.rsi_oversold})")
            
            # 2. MACD bullish
            if macd and macd > 0:
                confirmations += 1
                reasons.append("MACD bullish")
            
            # 3. Price near support (BB lower)
            if bb_lower and price and bb_lower > 0:
                distance = (price - bb_lower) / price
                if distance < 0.02:
                    confirmations += 1
                    reasons.append("Price near support")
            
            # 4. SPY bullish (required for stocks)
            if spy_bullish:
                confirmations += 1
                reasons.append("SPY above 20MA (bull market)")
            
            # 5. Low VIX
            if vix and vix < self.config.vix_max:
                confirmations += 1
                reasons.append(f"VIX low ({vix:.1f})")
            
            # 6. Volume spike
            if volume_ratio and volume_ratio > 1.3:
                confirmations += 1
                reasons.append(f"Volume spike ({volume_ratio:.1f}x)")
            
            # 7. Price above EMA20
            if price and ema20 and price > ema20:
                confirmations += 1
                reasons.append("Price above EMA20")
        
        elif direction == "DOWN":
            # 1. RSI overbought (> 65 for stocks)
            if rsi and rsi > self.config.rsi_overbought:
                confirmations += 1
                reasons.append(f"RSI overbought ({rsi:.0f} > {self.config.rsi_overbought})")
            
            # 2. MACD bearish
            if macd and macd < 0:
                confirmations += 1
                reasons.append("MACD bearish")
            
            # 3. Price near resistance (BB upper)
            if bb_upper and price and bb_upper > 0:
                distance = (bb_upper - price) / price
                if distance < 0.02:
                    confirmations += 1
                    reasons.append("Price near resistance")
            
            # 4. SPY bearish
            if not spy_bullish:
                confirmations += 1
                reasons.append("SPY below 20MA (bear market)")
            
            # 5. High VIX
            if vix and vix > 25:
                confirmations += 1
                reasons.append(f"VIX elevated ({vix:.1f})")
        
        return confirmations, reasons
    
    async def predict(self, symbol: str, bypass_calendar: bool = False) -> StockPrediction:
        """
        Generate stock prediction using stock-tuned model.
        
        This is the main entry point for stock predictions.
        
        Args:
            symbol: Stock ticker
            bypass_calendar: If True, skip FOMC/CPI/NFP blackout gates (for testing only)
        """
        if not self._initialized:
            await self.initialize()
        
        LOGGER.info(f"🏛️ Stock Engine predicting {symbol}{'[BYPASS CALENDAR]' if bypass_calendar else ''}...")
        
        gates_passed = []
        gates_failed = []
        all_reasons = []
        
        # Step 1: Economic Calendar Gate (FOMC, CPI, NFP, Earnings)
        if not bypass_calendar:
            try:
                from core.economic_calendar import economic_calendar_gate
                allow, reason = economic_calendar_gate(symbol)
                if not allow:
                    gates_failed.append(f"EconomicCalendar: {reason}")
                    return StockPrediction(
                        symbol=symbol,
                        direction="HOLD",
                        confidence=0.0,
                        entry_price=0,
                        target_price=0,
                        stop_loss=0,
                        horizon_hours=self.config.horizon_hours,
                        confirmations=0,
                        gates_passed=gates_passed,
                        gates_failed=gates_failed,
                        reasons=[f"BLOCKED: {reason}"]
                    )
                gates_passed.append("EconomicCalendar")
            except ImportError:
                LOGGER.warning("Economic calendar gate not available")
        else:
            gates_passed.append("EconomicCalendar (BYPASSED for testing)")
        
        # Step 2: Get VIX
        vix = await self._get_vix()
        if vix and vix > self.config.vix_max + 5:  # Hard block at VIX > 25
            gates_failed.append(f"VIX: {vix:.1f} > {self.config.vix_max + 5}")
            return StockPrediction(
                symbol=symbol,
                direction="HOLD",
                confidence=0.0,
                entry_price=0,
                target_price=0,
                stop_loss=0,
                horizon_hours=self.config.horizon_hours,
                confirmations=0,
                gates_passed=gates_passed,
                gates_failed=gates_failed,
                reasons=[f"VIX too high ({vix:.1f})"]
            )
        if vix and vix < self.config.vix_max:
            gates_passed.append(f"VIX ({vix:.1f})")
        
        # Step 3: SPY Regime Gate
        spy_bullish, spy_pct = await self._get_spy_regime()
        if self.config.require_spy_bull and not spy_bullish and spy_pct < -2:
            gates_failed.append(f"SPYRegime: {spy_pct:+.1f}% vs MA")
            return StockPrediction(
                symbol=symbol,
                direction="HOLD",
                confidence=0.0,
                entry_price=0,
                target_price=0,
                stop_loss=0,
                horizon_hours=self.config.horizon_hours,
                confirmations=0,
                gates_passed=gates_passed,
                gates_failed=gates_failed,
                reasons=[f"Bear market (SPY {spy_pct:+.1f}% vs 20MA)"]
            )
        if spy_bullish:
            gates_passed.append(f"SPYRegime ({spy_pct:+.1f}%)")
        
        # Step 4: Sector Momentum Gate
        try:
            from core.sector_momentum import sector_momentum_gate
            sector_allowed, sector_reason, sector_modifier = sector_momentum_gate(symbol, "UP")
            if not sector_allowed:
                gates_failed.append(f"SectorMomentum: {sector_reason}")
            else:
                gates_passed.append(f"SectorMomentum: {sector_reason}")
                all_reasons.append(sector_reason)
        except ImportError:
            sector_modifier = 1.0
            LOGGER.warning("Sector momentum gate not available")
        
        # Step 5: Get Technical Indicators
        indicators = await self._get_technical_indicators(symbol)
        price = indicators.get("current_price", 0)
        
        if not price or price <= 0:
            return StockPrediction(
                symbol=symbol,
                direction="HOLD",
                confidence=0.0,
                entry_price=0,
                target_price=0,
                stop_loss=0,
                horizon_hours=self.config.horizon_hours,
                confirmations=0,
                gates_passed=gates_passed,
                gates_failed=["Price unavailable"],
                reasons=["Could not get current price"]
            )
        
        # Step 6: Determine Direction using ENSEMBLE + Technical Indicators
        # Use None-safe defaults — None means "indicator unavailable, don't use"
        rsi = indicators.get("rsi_14")  # None if unavailable
        macd = indicators.get("macd_histogram")  # None if unavailable
        price = indicators.get("current_price", 0)
        
        # PRIMARY: Use Ensemble Predictor (LSTM + XGBoost + Transformer)
        # This was missing - stocks need the same ML power as crypto
        direction = "HOLD"
        ensemble_confidence = 0.5
        try:
            from core.ensemble_predictor import get_ensemble_predictor
            from core.data_pillars.feature_orchestrator import get_feature_orchestrator
            
            # Get full feature set for ensemble
            orchestrator = get_feature_orchestrator()
            feature_data = orchestrator.get_all_features(symbol, period=90)
            features = feature_data.get("features", {})
            
            ensemble = get_ensemble_predictor()
            ensemble_result = ensemble.predict(features, method="confidence_weighted", symbol=symbol)
            
            if ensemble_result.confidence > 0.45:
                direction = ensemble_result.direction
                ensemble_confidence = ensemble_result.confidence
                all_reasons.append(f"Ensemble: {direction} ({ensemble_confidence:.0%})")
                LOGGER.info(f"🏛️ [{symbol}] Ensemble: {direction} ({ensemble_confidence:.0%})")
        except Exception as e:
            LOGGER.warning(f"[{symbol}] Ensemble predictor failed: {e}")
        
        # FALLBACK: Technical indicators if ensemble is uncertain
        # Only use RSI/MACD if they are REAL values (not None defaults)
        if direction == "HOLD" or ensemble_confidence < 0.55:
            if rsi is not None and macd is not None:
                if rsi < 40 and macd > 0:
                    direction = "UP"
                    all_reasons.append(f"RSI oversold ({rsi:.0f}) + MACD bullish")
                elif rsi > 60 and macd < 0:
                    direction = "DOWN"
                    all_reasons.append(f"RSI overbought ({rsi:.0f}) + MACD bearish")
                elif rsi < 50 and macd > 0 and spy_bullish:
                    direction = "UP"
                    all_reasons.append("MACD bullish + SPY bull regime")
                elif rsi > 50 and macd < 0 and not spy_bullish:
                    direction = "DOWN"
                    all_reasons.append("MACD bearish + SPY bear regime")
            else:
                LOGGER.warning(f"[{symbol}] RSI/MACD unavailable — cannot use technical fallback")
        
        # Step 7: Get News Sentiment
        news_sentiment = 0.0
        news_score_label = "NEUTRAL"
        try:
            from core.news_sentiment import fetch_news_sentiment
            news_data = fetch_news_sentiment(symbol, limit=10)
            if news_data.get("ok"):
                news_sentiment = news_data.get("sentiment_score", 0.0)
                news_score_label = news_data.get("sentiment_label", "NEUTRAL")
                if abs(news_sentiment) > 0.3:
                    all_reasons.append(f"News: {news_score_label} ({news_sentiment:+.2f})")
                LOGGER.info(f"🏛️ [{symbol}] News sentiment: {news_score_label} ({news_sentiment:+.2f})")
        except Exception as e:
            LOGGER.warning(f"[{symbol}] News sentiment failed: {e}")

        # Step 7.5: WOLF-Specific Context (short interest, earnings, EDGAR, competitors)
        # Only runs when predicting WOLF — zero overhead for any other symbol (N/A here since
        # Ghost is now WOLF-only, but the guard keeps this clean if the symbol ever changes).
        wolf_context_adj = 0.0
        if symbol.upper() == "WOLF":
            try:
                from core.wolf_context import get_wolf_context
                wctx = get_wolf_context(direction=direction)
                wolf_context_adj = wctx.net_confidence_adj
                if wctx.reasons:
                    all_reasons.extend(wctx.reasons[:3])
                    LOGGER.info(
                        f"🐺 [WOLF] Context adj={wolf_context_adj:+.3f}: {wctx.reasons}"
                    )
                # Earnings caution: override to HOLD if caution_mode is active
                # (earnings within 2 days = too binary, Ghost shouldn't bet)
                if wctx.earnings and wctx.earnings.caution_mode and direction != "HOLD":
                    LOGGER.warning(
                        f"🐺 [WOLF] Earnings in {wctx.earnings.days_away}d — forcing HOLD"
                    )
                    direction = "HOLD"
            except Exception as e:
                LOGGER.warning(f"[WOLF] Context fetch failed: {e}")

        # Step 8: Apply Ghost Intel Rules (CRITICAL FIX - was missing!)
        intel_boost = 0.0
        try:
            from ghost_intel.integration import apply_intel_to_prediction
            
            # Apply Intel rules to stock prediction
            intel_direction, intel_confidence, intel_meta = apply_intel_to_prediction(
                symbol=symbol,
                direction=direction,
                confidence=ensemble_confidence
            )
            
            if intel_meta.get("intel_applied"):
                intel_boost = intel_meta.get("confidence_adjustment", 0)
                signals = intel_meta.get("signal_sources", [])
                
                # Intel can override direction in strong cases
                if intel_meta.get("direction_override") and intel_direction != direction:
                    direction = intel_direction
                    all_reasons.append(f"Intel override: {direction}")
                
                if signals:
                    all_reasons.extend([f"Intel: {s}" for s in signals[:3]])
                
                LOGGER.info(f"🏛️ [{symbol}] Intel applied: boost={intel_boost:+.0%}, signals={signals[:3]}")
        except Exception as e:
            LOGGER.warning(f"[{symbol}] Intel integration failed: {e}")
        
        # Step 8: Count Confirmations
        confirmations, confirmation_reasons = await self._count_confirmations(
            symbol, direction, indicators, vix or 20, spy_bullish
        )
        all_reasons.extend(confirmation_reasons)
        
        # Step 9: Multi-Timeframe Check
        try:
            from core.stock_gates import StockConfirmationCounter
            # MTF adds to confirmations
            mtf_confirms = 0  # Would come from multi_timeframe module
            confirmations += mtf_confirms
        except ImportError:
            pass
        
        # Step 10: Calculate Confidence (incorporating ensemble + Intel)
        # USE THE REAL CONFIDENCE - don't floor at 50%
        # If the model says 35%, that's valuable information (low conviction = don't trade)
        base_confidence = ensemble_confidence  # Raw model output, no floor
        
        # ================================================================
        # PRE-MARKET CONFIDENCE FLOOR (restored from lost commit 3fb0b14)
        #
        # At 8 AM CT (9 AM ET), the ensemble predictor is degraded because
        # the feature orchestrator can't get real-time price/volume data.
        # But the stock engine's indicators (from Polygon daily bars) are valid.
        #
        # Instead of a flat boost (which never hits V3 0.68 floor), apply
        # a FLOOR based on confirmations.  This ensures stocks with strong
        # multi-indicator agreement reach the TOP 10 card, while weak
        # signals correctly fail:
        #   0 confirms = 0.62 (fails V3)
        #   1 confirm  = 0.65 (fails V3)
        #   2 confirms = 0.68 (borderline)
        #   3 confirms = 0.71 (passes V3 ✅)
        #   4 confirms = 0.74 (passes V3 ✅)
        #   5 confirms = 0.77 (passes V3 ✅)
        #
        # Completely inert during market hours (live quotes available).
        # ================================================================
        premarket = _is_premarket()
        data_quality = indicators.get("data_quality_score", 1.0)
        
        if premarket and direction != "HOLD" and data_quality >= 0.7:
            premarket_floor = 0.62 + (confirmations * 0.03)
            if base_confidence < premarket_floor:
                LOGGER.info(
                    f"🌅 [{symbol}] Pre-market confidence floor: "
                    f"{base_confidence:.0%} → {premarket_floor:.0%} "
                    f"({confirmations} confirms, data_quality={data_quality:.0%})"
                )
                base_confidence = premarket_floor
        
        # Boost for confirmations (NOT double-counted with pre-market)
        # Reduced from 0.04 to 0.02 per confirmation, cap 0.10 (was 0.25)
        conf_boost = min(0.10, confirmations * 0.02)
        
        # Intel boost (already calculated)
        intel_adj = intel_boost
        
        # News sentiment boost/penalty
        news_adj = 0.0
        if abs(news_sentiment) > 0.3:  # Only apply if sentiment is strong
            if direction == "UP" and news_sentiment > 0.3:
                news_adj = min(0.05, news_sentiment * 0.08)  # Max +5% for very bullish news
            elif direction == "DOWN" and news_sentiment < -0.3:
                news_adj = min(0.05, abs(news_sentiment) * 0.08)  # Max +5% for very bearish news
            elif direction == "UP" and news_sentiment < -0.3:
                news_adj = -0.03  # Penalty if news contradicts direction
            elif direction == "DOWN" and news_sentiment > 0.3:
                news_adj = -0.03
        
        # Boost/penalty for sector (only if sector gate ran)
        try:
            sector_adj = (sector_modifier - 1.0) * 0.1
        except NameError:
            sector_adj = 0
        
        # Regime-aware adjustment (boost in favorable regimes)
        regime_adj = 0.0
        try:
            from core.regime_detector import RegimeDetector
            
            # Get recent prices for SPY to detect regime
            # Use cached detector result if available
            import state
            regime_cache_key = "regime_detector_cache"
            regime_cache_ttl = 300  # 5 minutes
            
            cached = getattr(state, regime_cache_key, None)
            if cached and (datetime.utcnow() - cached["timestamp"]).total_seconds() < regime_cache_ttl:
                regime_result = cached["result"]
            else:
                # Fetch SPY prices from indicators (last 50 periods for regime detection)
                spy_prices = []
                try:
                    # Get SPY price history from polygon or indicators
                    spy_indicators = self.get_indicators("SPY", "stock")
                    if spy_indicators and "close_prices" in spy_indicators:
                        spy_prices = spy_indicators["close_prices"][-50:]  # Last 50 periods
                    
                    # Fallback: try to get current price
                    if not spy_prices:
                        spy_quote = self.get_quote("SPY", "stock")
                        if spy_quote and spy_quote.get("price"):
                            # Use single price point with SIDEWAYS default
                            regime_result = {"regime": "SIDEWAYS", "confidence": 0.5}
                        else:
                            regime_result = {"regime": "SIDEWAYS", "confidence": 0.5}
                    else:
                        # Have price history - run regime detection
                        detector = RegimeDetector()
                        regime_result = detector.detect_regime(spy_prices)
                    
                    setattr(state, regime_cache_key, {"result": regime_result, "timestamp": datetime.utcnow()})
                except Exception as spy_err:
                    LOGGER.debug(f"Could not fetch SPY prices for regime detection: {spy_err}")
                    regime_result = {"regime": "SIDEWAYS", "confidence": 0.5}
                    setattr(state, regime_cache_key, {"result": regime_result, "timestamp": datetime.utcnow()})
            
            regime = regime_result.get("regime", "SIDEWAYS")
            regime_confidence = regime_result.get("confidence", 0.5)
            
            # Apply regime-aware adjustments
            if regime == "BULL" and direction == "UP":
                regime_adj = 0.03 * regime_confidence  # Boost UP signals in bull market
                all_reasons.append(f"Bull regime (+{regime_adj:.1%})")
            elif regime == "BEAR" and direction == "DOWN":
                regime_adj = 0.03 * regime_confidence  # Boost DOWN signals in bear market
                all_reasons.append(f"Bear regime (+{regime_adj:.1%})")
            elif regime == "VOLATILE":
                regime_adj = -0.02  # Reduce confidence in volatile regimes
                all_reasons.append(f"Volatile regime ({regime_adj:.1%})")
            
            if regime != "SIDEWAYS":  # Only log non-default regimes
                LOGGER.info(f"[{symbol}] Regime: {regime} (conf={regime_confidence:.0%}), adj={regime_adj:+.1%}")
        except Exception as e:
            LOGGER.debug(f"[{symbol}] Regime detector skipped: {e}")
            regime_adj = 0.0
        
        # Penalty for high VIX
        vix_penalty = max(0, (vix - 15) * 0.01) if vix else 0

        confidence = base_confidence + conf_boost + intel_adj + news_adj + sector_adj + regime_adj + wolf_context_adj - vix_penalty
        # HARD CAP at 85% - we only have 52% win rate, no justification for higher
        confidence = max(0.1, min(0.85, confidence))
        
        # Step 10: Calculate Entry/Exit
        if direction == "UP":
            entry_price = price
            target_price = price * (1 + self.config.target_pct / 100)
            stop_loss = price * (1 - self.config.stop_loss_pct / 100)
        elif direction == "DOWN":
            entry_price = price
            target_price = price * (1 - self.config.target_pct / 100)
            stop_loss = price * (1 + self.config.stop_loss_pct / 100)
        else:
            entry_price = price
            target_price = price
            stop_loss = price
        
        # VALIDATION: Check risk/reward ratio
        # For UP: reward = target-entry, risk = entry-stop
        # For DOWN: reward = entry-target, risk = stop-entry
        if direction == "UP":
            reward = target_price - entry_price
            risk = entry_price - stop_loss
        elif direction == "DOWN":
            reward = entry_price - target_price
            risk = stop_loss - entry_price
        else:
            reward = 0
            risk = 0
        
        if risk > 0 and reward > 0:
            risk_reward_ratio = reward / risk
            if risk_reward_ratio < 1.0:
                LOGGER.warning(
                    f"⚠️ [{symbol}] Poor risk/reward {risk_reward_ratio:.2f}:1 "
                    f"(reward={reward:.2f}, risk={risk:.2f}) - consider rejecting"
                )
                # Optionally reduce confidence for bad R:R
                if risk_reward_ratio < 0.75:
                    confidence = max(0.1, confidence * 0.9)  # 10% penalty
                    LOGGER.info(f"🔻 [{symbol}] Confidence penalized to {confidence:.0%} due to bad R:R")
        
        # Step 11: Magnitude Estimation (HOW MUCH will it move?)
        # Uses ATR + volatility + confidence to estimate expected % move
        atr_pct = indicators.get("atr_pct", 0)
        daily_vol = indicators.get("daily_volatility_pct", 1.5)
        recent_5d = indicators.get("recent_5d_return_pct", 0)
        
        # Base magnitude from ATR (normalized to hold period)
        # ATR is daily, scale by sqrt(hold_days) for multi-day estimates
        hold_days = self.config.horizon_hours / 24
        magnitude_from_atr = atr_pct * (hold_days ** 0.5)  # Sqrt scaling
        
        # Confidence multiplier: higher confidence = larger expected move
        conf_multiplier = 0.5 + (confidence * 0.5)  # Range 0.55 to 0.925
        
        # Momentum adjustment: if recent trend aligns, boost magnitude
        momentum_adj = 1.0
        if direction == "UP" and recent_5d > 1.0:
            momentum_adj = 1.15  # Momentum + direction aligned
        elif direction == "DOWN" and recent_5d < -1.0:
            momentum_adj = 1.15
        elif (direction == "UP" and recent_5d < -2.0) or (direction == "DOWN" and recent_5d > 2.0):
            momentum_adj = 0.85  # Counter-trend, reduce expectation
        
        # Data quality affects confidence: bad data = lower confidence
        # PRE-MARKET EXCEPTION: Skip penalty when daily-bar indicators are good.
        # At 8 AM CT, data_quality from _get_technical_indicators() reflects
        # daily bars (RSI, MACD, BB, volume, ATR) — these are fully valid
        # pre-market.  Only penalize during market hours when stale data is
        # genuinely a problem (live quotes should be available).
        if data_quality < 0.5 and not (premarket and data_quality >= 0.3):
            # Less than half the indicators are real - scale confidence DOWN
            quality_penalty = (0.5 - data_quality) * 0.3  # Up to -15% penalty
            confidence = max(0.1, confidence - quality_penalty)
            LOGGER.warning(
                f"⚠️ [{symbol}] Data quality {data_quality:.0%} → confidence penalized by {quality_penalty:.0%}"
            )
        elif premarket and data_quality < 0.5:
            LOGGER.info(
                f"🌅 [{symbol}] Pre-market: skipping data quality penalty "
                f"(quality={data_quality:.0%}, daily bars accepted as valid)"
            )
        
        # Final expected move (conservative: cap at 2x ATR)
        expected_move_pct = min(magnitude_from_atr * conf_multiplier * momentum_adj, atr_pct * 2 * hold_days)
        expected_move_pct = max(0.5, expected_move_pct)  # Floor at 0.5%
        
        # Adjust target_price based on magnitude
        if direction == "UP":
            target_price = price * (1 + expected_move_pct / 100)
        elif direction == "DOWN":
            target_price = price * (1 - expected_move_pct / 100)
        
        # Step 12: Calculate Position Sizing (Kelly Criterion)
        position_size_usd = 0
        shares = 0
        try:
            from core.position_sizer import get_position_sizer
            sizer = get_position_sizer()
            # Use recent paper trade stats for win_rate
            win_rate = 0.56  # Current paper trading win rate
            avg_win = expected_move_pct if expected_move_pct > 0 else 3.0
            avg_loss = (abs(entry_price - stop_loss) / entry_price) * 100
            
            sizing = sizer.calculate_position_size(
                symbol=symbol,
                entry_price=entry_price,
                confidence=confidence,
                atr=atr_pct * entry_price / 100,  # Convert ATR % to $
                win_rate=win_rate,
                avg_win_pct=avg_win,
                avg_loss_pct=avg_loss
            )
            position_size_usd = sizing.dollar_amount
            shares = sizing.shares
            LOGGER.info(f"🏛️ [{symbol}] Position size: {shares} shares (${position_size_usd:.0f})")
        except Exception as e:
            LOGGER.warning(f"[{symbol}] Position sizing failed: {e}")
            position_size_usd = 1000  # Default $1000
            shares = int(1000 / entry_price) if entry_price > 0 else 0
        
        # Step 13: Build Comprehensive Explanation
        explanation = f"{direction} signal with {confidence:.0%} confidence. "
        if all_reasons:
            explanation += "Key factors: " + ", ".join(all_reasons[:3]) + ". "
        if abs(news_sentiment) > 0.3:
            explanation += f"News sentiment is {news_score_label.lower()}. "
        explanation += f"Expected move: {expected_move_pct:.1f}% over {int(self.config.horizon_hours/24)} days."
        
        # Final prediction
        prediction = StockPrediction(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            entry_price=entry_price,
            target_price=target_price,
            stop_loss=stop_loss,
            horizon_hours=self.config.horizon_hours,
            confirmations=confirmations,
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            reasons=all_reasons[:5],  # Top 5 reasons
            expected_move_pct=round(expected_move_pct, 2),
            atr_pct=round(atr_pct, 2),
            data_quality=round(data_quality, 2),
            explanation=explanation,
            position_size_usd=round(position_size_usd, 2),
            shares=shares,
        )
        
        LOGGER.info(
            f"🏛️ {symbol} → {direction} ({confidence:.0%}) | "
            f"{confirmations} confirmations | expected move: {expected_move_pct:+.1f}% | "
            f"ATR: {atr_pct:.1f}%{' | 🌅 PRE-MARKET' if premarket else ''}"
        )
        
        # Record pattern for accuracy tracking (only actionable predictions)
        if direction in ("UP", "DOWN") and confidence >= 0.6:
            try:
                record_pattern_detection(
                    pattern_type=f"stock_{direction.lower()}",
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    confidence=confidence
                )
            except Exception as e:
                LOGGER.warning(f"[PATTERN_TRACKER] Failed to record: {e}")
        
        # NOTE: Paper trade logging is handled centrally in wolf_app.py run_prediction
        # which calls paper_tracker.log_signal() with centralized dedup.
        # Previously this created DUPLICATE trades (stock_engine + run_prediction both logging).
        # Removed Feb 2026 to fix 793 trades/day volume (should be ~25/cycle).
        
        return prediction
    
    async def predict_batch(self, symbols: List[str], bypass_calendar: bool = False) -> Dict[str, StockPrediction]:
        """Predict multiple stocks in parallel
        
        Args:
            symbols: List of stock symbols to predict
            bypass_calendar: If True, skip FOMC/CPI/NFP blackout checks (for testing)
        """
        if not self._initialized:
            await self.initialize()
        
        tasks = [self.predict(s, bypass_calendar=bypass_calendar) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        predictions = {}
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                LOGGER.error(f"Prediction failed for {symbol}: {result}")
                predictions[symbol] = StockPrediction(
                    symbol=symbol,
                    direction="HOLD",
                    confidence=0,
                    entry_price=0,
                    target_price=0,
                    stop_loss=0,
                    horizon_hours=self.config.horizon_hours,
                    confirmations=0,
                    gates_passed=[],
                    gates_failed=[str(result)],
                    reasons=["Prediction failed"]
                )
            else:
                predictions[symbol] = result
        
        return predictions


# Singleton instance
_stock_engine: Optional[StockEngine] = None


def get_stock_engine() -> StockEngine:
    """Get or create the singleton stock engine"""
    global _stock_engine
    if _stock_engine is None:
        _stock_engine = StockEngine()
    return _stock_engine


async def run_stock_prediction(symbol: str) -> Dict[str, Any]:
    """
    Public API: Run stock prediction and return dict result.
    
    This is the main entry point from wolf_app.py
    """
    engine = get_stock_engine()
    prediction = await engine.predict(symbol)
    return prediction.to_dict()


# ============================================================================
# TESTING
# ============================================================================

if __name__ == "__main__":
    async def test():
        print("🏛️ Stock Engine Test")
        print("=" * 60)
        
        engine = StockEngine()
        await engine.initialize()
        
        print(f"\nConfig: {engine.config.to_dict()}")
        
        test_symbols = ["AAPL", "MSFT", "JPM"]
        
        for symbol in test_symbols:
            print(f"\n{'='*60}")
            print(f"Testing {symbol}:")
            print("-" * 40)
            
            prediction = await engine.predict(symbol)
            
            print(f"Direction: {prediction.direction}")
            print(f"Confidence: {prediction.confidence:.1%}")
            print(f"Entry: ${prediction.entry_price:.2f}")
            print(f"Target: ${prediction.target_price:.2f}")
            print(f"Stop: ${prediction.stop_loss:.2f}")
            print(f"Confirmations: {prediction.confirmations}/{engine.config.min_confirmations}")
            print(f"Actionable: {prediction.is_actionable}")
            
            print(f"\nGates Passed: {prediction.gates_passed}")
            print(f"Gates Failed: {prediction.gates_failed}")
            print(f"Reasons: {prediction.reasons}")
    
    asyncio.run(test())
