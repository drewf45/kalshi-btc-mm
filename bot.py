"""
Kalshi Bitcoin 15-Min Range Trading Bot
Strategy: Aggressive entry + Smart dump exits
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, List
import json
import base64

import requests
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import jwt

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Bot configuration - edit these values"""
    
    # API Configuration
    KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
    KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
    KALSHI_PRIVATE_KEY_PEM_BASE64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64")
    
    # Market Configuration
    SERIES = os.getenv("SERIES", "KXBTC15M")  # Bitcoin 15-minute markets
    MARKET_OVERRIDE = os.getenv("MARKET_OVERRIDE")  # For testing specific market
    
    # Trading Switches
    DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
    ENABLE_TRADING = os.getenv("ENABLE_TRADING", "True").lower() == "true"
    POST_ONLY = os.getenv("POST_ONLY", "False").lower() == "true"
    ONE_TRADE_PER_MARKET = True  # Prevent doubling down
    
    # Entry Strategy - AGGRESSIVE
    ARM_TIME_SEC = int(os.getenv("ARM_TIME_SEC", "600"))  # Arm 10 min before close
    STOP_NEW_ENTRY_SEC = int(os.getenv("STOP_NEW_ENTRY_SEC", "45"))  # No entries <45s
    PROB_MIN = float(os.getenv("PROB_MIN", "0.60"))  # Lower threshold - trade more
    EDGE_MIN = float(os.getenv("EDGE_MIN", "0.015"))  # 1.5% minimum edge
    MAX_ENTRY_PRICE_CENTS = int(os.getenv("MAX_ENTRY_PRICE_CENTS", "85"))  # Max 85¢
    
    # Dynamic edge by time remaining
    EARLY_EDGE_MIN = float(os.getenv("EARLY_EDGE_MIN", "0.025"))  # 2.5% if >5min
    LATE_EDGE_MIN = float(os.getenv("LATE_EDGE_MIN", "0.015"))   # 1.5% if <2min
    
    # Position Sizing - MORE AGGRESSIVE
    BANKROLL_FRACTION = float(os.getenv("BANKROLL_FRACTION", "0.10"))  # 10% per trade
    BANKROLL_FRACTION_HARD_CAP = float(os.getenv("BANKROLL_FRACTION_HARD_CAP", "0.25"))
    
    # Exit Strategy - DUMP CONFIGURATION
    ENABLE_DUMP = os.getenv("ENABLE_DUMP", "True").lower() == "true"
    DUMP_PROB_FLIP = float(os.getenv("DUMP_PROB_FLIP", "0.45"))  # Exit if <45%
    DUMP_PROB_DROP_PERCENT = float(os.getenv("DUMP_PROB_DROP_PERCENT", "0.20"))  # Exit if -20%
    DUMP_MARKET_FLIP_THRESHOLD = float(os.getenv("DUMP_MARKET_FLIP_THRESHOLD", "0.30"))
    DUMP_STOP_LOSS_PERCENT = float(os.getenv("DUMP_STOP_LOSS_PERCENT", "0.60"))  # -60% stop
    DUMP_MIN_TIME_REMAINING = int(os.getenv("DUMP_MIN_TIME_REMAINING", "30"))  # Never dump <30s
    
    # Bitcoin price-based dump
    DUMP_ON_PRICE_DANGER = os.getenv("DUMP_ON_PRICE_DANGER", "True").lower() == "true"
    DUMP_PRICE_SIGMA_MULTIPLIER = float(os.getenv("DUMP_PRICE_SIGMA_MULTIPLIER", "1.5"))
    
    # Model Configuration
    USE_MARKET_IMPLIED = True
    MODEL_BLEND_ALPHA = float(os.getenv("MODEL_BLEND_ALPHA", "0.75"))  # Weight on our model
    USE_DYNAMIC_SIGMA = True
    
    # Bitcoin spot price source
    BTC_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    
    # Logging
    HEARTBEAT_SECONDS = float(os.getenv("HEARTBEAT_SECONDS", "15.0"))

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================================
# DATA MODELS
# ============================================================================

class State(Enum):
    """Bot state machine"""
    IDLE = "IDLE"           # Waiting to arm
    ARMED = "ARMED"         # Monitoring, ready to enter
    HOLDING = "HOLDING"     # Position held
    DUMPED = "DUMPED"       # Exited position early
    SKIP = "SKIP"           # Skipped this market
    SETTLED = "SETTLED"     # Market closed

@dataclass
class Position:
    """Current position in market"""
    market_id: str
    direction: str  # "YES" or "NO"
    quantity: int
    entry_price_cents: int
    entry_time: float
    entry_model_prob: float
    entry_market_prob: float
    entry_spot_price: float

@dataclass
class OrderBook:
    """Market order book snapshot"""
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    
    def midpoint_probability(self) -> float:
        """Calculate implied probability from order book"""
        if self.yes_bid and self.yes_ask:
            return ((self.yes_bid + self.yes_ask) / 2) / 100
        elif self.yes_bid:
            return self.yes_bid / 100
        elif self.yes_ask:
            return self.yes_ask / 100
        return 0.5

@dataclass
class EntryOpportunity:
    """Evaluated entry opportunity"""
    should_trade: bool
    direction: Optional[str] = None
    entry_price: Optional[int] = None
    edge: float = 0.0
    model_prob: float = 0.0
    market_prob: float = 0.0
    quantity: int = 0
    reason: str = ""

@dataclass
class DumpDecision:
    """Dump decision result"""
    should_dump: bool
    reason: Optional[str] = None

# ============================================================================
# KALSHI API CLIENT
# ============================================================================

class KalshiClient:
    """Kalshi API client with authentication"""
    
    def __init__(self):
        self.base_url = Config.KALSHI_API_BASE.strip()
        self.api_key_id = Config.KALSHI_API_KEY_ID
        self.private_key = self._load_private_key()
        self.session = requests.Session()
        
    def _load_private_key(self):
        """Load private key from base64 env var"""
        pem_base64 = Config.KALSHI_PRIVATE_KEY_PEM_BASE64
        if not pem_base64:
            raise ValueError("KALSHI_PRIVATE_KEY_PEM_BASE64 not set")
        
        pem_bytes = base64.b64decode(pem_base64)
        return serialization.load_pem_private_key(
            pem_bytes,
            password=None,
            backend=default_backend()
        )
    
    def _generate_jwt(self) -> str:
        """Generate JWT for API authentication"""
        now = int(time.time())
        payload = {
            "sub": self.api_key_id,
            "iss": self.api_key_id,
            "iat": now,
            "exp": now + 300,  # 5 minute expiry
            "aud": "kalshi.com"
        }
        
        return jwt.encode(
            payload,
            self.private_key,
            algorithm="RS256"
        )
    
    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make authenticated API request"""
        url = f"{self.base_url}{endpoint}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._generate_jwt()}"
        
        response = self.session.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()
    
    def get_exchange_status(self) -> dict:
        """Get exchange status"""
        return self._request("GET", "/exchange/status")
    
    def get_balance(self) -> int:
        """Get account balance in cents"""
        data = self._request("GET", "/portfolio/balance")
        return data.get("balance", 0)
    
    def get_markets(self, series_ticker: str, status: str = "open") -> List[dict]:
        """Get markets for series"""
        params = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": 100
        }
        data = self._request("GET", "/markets", params=params)
        return data.get("markets", [])
    
    def get_market(self, market_ticker: str) -> dict:
        """Get single market details"""
        data = self._request("GET", f"/markets/{market_ticker}")
        return data.get("market", {})
    
    def get_orderbook(self, market_ticker: str) -> dict:
        """Get market orderbook"""
        data = self._request("GET", f"/markets/{market_ticker}/orderbook")
        return data.get("orderbook", {})
    
    def get_positions(self, market_ticker: Optional[str] = None) -> List[dict]:
        """Get current positions"""
        params = {}
        if market_ticker:
            params["ticker"] = market_ticker
        data = self._request("GET", "/portfolio/positions", params=params)
        return data.get("positions", [])
    
    def create_order(
        self,
        market_ticker: str,
        side: str,
        action: str,
        count: int,
        price: Optional[int] = None,
        order_type: str = "limit"
    ) -> dict:
        """Create order"""
        payload = {
            "ticker": market_ticker,
            "client_order_id": f"bot_{int(time.time() * 1000)}",
            "side": side,
            "action": action,
            "count": count,
            "type": order_type
        }
        
        if price is not None:
            payload["yes_price"] = price if side == "yes" else None
            payload["no_price"] = price if side == "no" else None
        
        if Config.DRY_RUN:
            logger.info(f"[DRY_RUN] Would create order: {payload}")
            return {"order": {"order_id": "dry_run"}}
        
        return self._request("POST", "/portfolio/orders", json=payload)
    
    def cancel_order(self, order_id: str) -> dict:
        """Cancel order"""
        if Config.DRY_RUN:
            logger.info(f"[DRY_RUN] Would cancel order: {order_id}")
            return {}
        
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

# ============================================================================
# BITCOIN PRICE FEED
# ============================================================================

class BitcoinPriceFeed:
    """Fetch Bitcoin spot price"""
    
    @staticmethod
    def get_spot_price() -> Optional[float]:
        """Get current BTC/USD spot price"""
        try:
            response = requests.get(Config.BTC_SPOT_URL, timeout=5)
            response.raise_for_status()
            data = response.json()
            price = float(data["data"]["amount"])
            return price
        except Exception as e:
            logger.error(f"[BTC] Failed to fetch spot price: {e}")
            return None

# ============================================================================
# PROBABILITY MODEL
# ============================================================================

class ProbabilityModel:
    """Calculate probability of Bitcoin being above/below threshold"""
    
    @staticmethod
    def calculate_sigma(time_to_close_sec: int) -> float:
        """
        Calculate expected volatility (sigma) based on time remaining
        Bitcoin volatility: ~80% annualized
        """
        if not Config.USE_DYNAMIC_SIGMA:
            return 100.0  # Fixed sigma
        
        # Annual volatility ~80%, convert to per-minute
        annual_vol = 0.80
        minutes_per_year = 365.25 * 24 * 60
        per_minute_vol = annual_vol / (minutes_per_year ** 0.5)
        
        # Scale by time remaining
        minutes_remaining = time_to_close_sec / 60
        sigma = per_minute_vol * (minutes_remaining ** 0.5)
        
        # Convert to dollar terms (rough approximation)
        # For BTC at $78k, 1% = $780
        current_price = 78000  # Approximate, update if needed
        sigma_dollars = sigma * current_price
        
        return max(sigma_dollars, 10.0)  # Minimum $10 sigma
    
    @staticmethod
    def calculate_probability(
        spot_price: float,
        threshold: float,
        time_to_close_sec: int,
        direction: str = "above"
    ) -> float:
        """
        Calculate probability of spot being above/below threshold at close
        Using simple normal distribution assumption
        """
        import math
        from scipy.stats import norm
        
        # Calculate sigma
        sigma = ProbabilityModel.calculate_sigma(time_to_close_sec)
        
        if sigma == 0:
            # No time left or no volatility
            if direction == "above":
                return 1.0 if spot_price > threshold else 0.0
            else:
                return 1.0 if spot_price < threshold else 0.0
        
        # Z-score: how many sigmas away from threshold
        z = (spot_price - threshold) / sigma
        
        # Probability of being above threshold
        prob_above = norm.cdf(z)
        
        if direction == "above":
            return prob_above
        else:
            return 1.0 - prob_above
    
    @staticmethod
    def blend_probabilities(
        model_prob: float,
        market_prob: float,
        alpha: float = None
    ) -> float:
        """
        Blend model probability with market probability
        alpha = 1.0: use only model
        alpha = 0.0: use only market
        """
        if alpha is None:
            alpha = Config.MODEL_BLEND_ALPHA
        
        return alpha * model_prob + (1 - alpha) * market_prob

# ============================================================================
# TRADING ENGINE
# ============================================================================

class TradingEngine:
    """Core trading logic"""
    
    def __init__(self, client: KalshiClient):
        self.client = client
        self.state = State.IDLE
        self.position: Optional[Position] = None
        self.market_id: Optional[str] = None
        self.dump_log: List[dict] = []
        self.trade_log: List[dict] = []
        
    def get_time_to_close(self, market: dict) -> int:
        """Get seconds until market close"""
        close_time = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (close_time - now).total_seconds()
        return int(delta)
    
    def parse_market_threshold(self, market: dict) -> Optional[float]:
        """Extract threshold from market title"""
        # Example: "KXBTC15M-26FEB020945-45" means above $77,945
        # or market might have range_min/range_max
        
        subtitle = market.get("subtitle", "")
        # Parse from subtitle like "Will BTC be above $77,945?"
        
        # Fallback: use ranged_group_by_delta
        if "ranged_group_by_delta" in market:
            floor_value = market["ranged_group_by_delta"].get("floor_value")
            if floor_value:
                return float(floor_value) / 100  # Convert cents to dollars
        
        return None
    
    def get_orderbook_snapshot(self, market_id: str) -> OrderBook:
        """Get current orderbook"""
        try:
            ob_data = self.client.get_orderbook(market_id)
            
            return OrderBook(
                yes_bid=ob_data.get("yes", [{}])[0].get("price") if ob_data.get("yes") else None,
                yes_ask=ob_data.get("yes", [{}])[-1].get("price") if ob_data.get("yes") else None,
                no_bid=ob_data.get("no", [{}])[0].get("price") if ob_data.get("no") else None,
                no_ask=ob_data.get("no", [{}])[-1].get("price") if ob_data.get("no") else None,
            )
        except Exception as e:
            logger.error(f"[OB] Error fetching orderbook: {e}")
            return OrderBook()
    
    def check_dump_conditions(
        self,
        position: Position,
        p_model: float,
        p_market: float,
        spot_price: float,
        t_close: int,
        threshold: float,
        sigma: float
    ) -> DumpDecision:
        """
        Check if position should be dumped
        Returns: DumpDecision
        """
        
        if not Config.ENABLE_DUMP:
            return DumpDecision(False)
        
        # NEVER dump in final seconds
        if t_close < Config.DUMP_MIN_TIME_REMAINING:
            return DumpDecision(False, "too_close_to_settlement")
        
        entry_dir = position.direction
        entry_prob = position.entry_model_prob
        
        # Get current probability for our direction
        current_prob = p_model if entry_dir == "YES" else (1 - p_model)
        
        # TRIGGER 1: Probability flipped below threshold
        if current_prob < Config.DUMP_PROB_FLIP:
            return DumpDecision(True, f"prob_flip_{current_prob:.3f}")
        
        # TRIGGER 2: Probability dropped significantly
        prob_drop = entry_prob - current_prob
        if prob_drop > Config.DUMP_PROB_DROP_PERCENT:
            return DumpDecision(True, f"prob_drop_{prob_drop:.3f}")
        
        # TRIGGER 3: Market probability flipped
        if Config.USE_MARKET_IMPLIED:
            market_prob = p_market if entry_dir == "YES" else (1 - p_market)
            if market_prob < Config.DUMP_MARKET_FLIP_THRESHOLD:
                return DumpDecision(True, f"market_flip_{market_prob:.3f}")
        
        # TRIGGER 4: Bitcoin price danger zone
        if Config.DUMP_ON_PRICE_DANGER:
            if entry_dir == "YES":
                # We bet BTC will be ABOVE threshold
                danger_price = threshold - (sigma * Config.DUMP_PRICE_SIGMA_MULTIPLIER)
                if spot_price < danger_price:
                    distance = threshold - spot_price
                    return DumpDecision(True, f"price_danger_YES_${distance:.0f}_below")
            
            elif entry_dir == "NO":
                # We bet BTC will be BELOW threshold
                danger_price = threshold + (sigma * Config.DUMP_PRICE_SIGMA_MULTIPLIER)
                if spot_price > danger_price:
                    distance = spot_price - threshold
                    return DumpDecision(True, f"price_danger_NO_${distance:.0f}_above")
        
        # ALL GOOD - HOLD
        return DumpDecision(False)
    
    def execute_dump(self, position: Position, reason: str, orderbook: OrderBook):
        """Execute dump - exit position"""
        
        logger.warning(f"[DUMP] {position.market_id} reason={reason} dir={position.direction} qty={position.quantity}")
        
        try:
            # Determine exit price
            if position.direction == "YES":
                # Sell YES position
                exit_price = orderbook.yes_bid
                side = "yes"
                action = "sell"
            else:
                # Sell NO position (buy back YES)
                exit_price = orderbook.no_bid  
                side = "no"
                action = "sell"
            
            if not exit_price:
                logger.error(f"[DUMP] No exit liquidity for {position.direction}")
                return
            
            # Calculate P&L
            pnl_cents = (exit_price - position.entry_price_cents) * position.quantity
            pnl_dollars = pnl_cents / 100
            
            # Place market order to exit
            if not Config.DRY_RUN:
                order = self.client.create_order(
                    market_ticker=position.market_id,
                    side=side,
                    action=action,
                    count=position.quantity,
                    order_type="market"
                )
                logger.info(f"[DUMP] Order placed: {order.get('order', {}).get('order_id')}")
            
            # Log dump
            dump_entry = {
                "market": position.market_id,
                "direction": position.direction,
                "entry_price": position.entry_price_cents,
                "exit_price": exit_price,
                "quantity": position.quantity,
                "pnl_cents": pnl_cents,
                "pnl_dollars": pnl_dollars,
                "reason": reason,
                "entry_time": position.entry_time,
                "dump_time": time.time(),
                "time_held_sec": time.time() - position.entry_time
            }
            self.dump_log.append(dump_entry)
            
            logger.warning(f"[DUMP] P&L: ${pnl_dollars:.2f} ({pnl_cents:+d}¢)")
            
            # Clear position
            self.position = None
            self.state = State.DUMPED
            
        except Exception as e:
            logger.error(f"[DUMP] Error executing dump: {e}")
    
    def evaluate_entry(
        self,
        p_model: float,
        p_market: float,
        orderbook: OrderBook,
        t_close: int,
        spot_price: float,
        threshold: float
    ) -> EntryOpportunity:
        """
        Evaluate if we should enter YES or NO
        Returns best opportunity (or no trade)
        """
        
        # Determine required edge based on time
        if t_close > 300:  # >5 minutes
            required_edge = Config.EARLY_EDGE_MIN
            required_prob = 0.65
        elif t_close > 120:  # 2-5 minutes
            required_edge = Config.EDGE_MIN
            required_prob = Config.PROB_MIN
        else:  # <2 minutes
            required_edge = Config.LATE_EDGE_MIN
            required_prob = 0.55
        
        # Evaluate YES side
        yes_opp = self._evaluate_side(
            direction="YES",
            model_prob=p_model,
            market_prob=p_market,
            orderbook=orderbook,
            required_edge=required_edge,
            required_prob=required_prob
        )
        
        # Evaluate NO side
        no_opp = self._evaluate_side(
            direction="NO",
            model_prob=(1 - p_model),
            market_prob=(1 - p_market),
            orderbook=orderbook,
            required_edge=required_edge,
            required_prob=required_prob
        )
        
        # Pick best side
        if yes_opp.edge > no_opp.edge and yes_opp.edge > required_edge:
            return yes_opp
        elif no_opp.edge > required_edge:
            return no_opp
        else:
            return EntryOpportunity(should_trade=False, reason="no_edge")
    
    def _evaluate_side(
        self,
        direction: str,
        model_prob: float,
        market_prob: float,
        orderbook: OrderBook,
        required_edge: float,
        required_prob: float
    ) -> EntryOpportunity:
        """Evaluate single side (YES or NO)"""
        
        # Check probability threshold
        if model_prob < required_prob:
            return EntryOpportunity(
                should_trade=False,
                reason=f"prob_too_low_{model_prob:.3f}"
            )
        
        # Get entry price
        if direction == "YES":
            entry_price = orderbook.yes_ask
        else:
            entry_price = orderbook.no_ask
        
        if not entry_price:
            return EntryOpportunity(
                should_trade=False,
                reason="no_ask_price"
            )
        
        if entry_price > Config.MAX_ENTRY_PRICE_CENTS:
            return EntryOpportunity(
                should_trade=False,
                reason=f"price_too_high_{entry_price}"
            )
        
        # Calculate edge
        fair_price = model_prob * 100
        edge = (fair_price - entry_price) / 100
        
        if edge < required_edge:
            return EntryOpportunity(
                should_trade=False,
                reason=f"edge_too_low_{edge:.4f}"
            )
        
        # Calculate position size
        quantity = self._calculate_position_size(
            edge=edge,
            model_prob=model_prob,
            entry_price=entry_price
        )
        
        return EntryOpportunity(
            should_trade=True,
            direction=direction,
            entry_price=entry_price,
            edge=edge,
            model_prob=model_prob,
            market_prob=market_prob,
            quantity=quantity
        )
    
    def _calculate_position_size(
        self,
        edge: float,
        model_prob: float,
        entry_price: int
    ) -> int:
        """Calculate position size using Kelly-ish criterion"""
        
        try:
            balance = self.client.get_balance()
        except:
            balance = 100000  # Default $1000 if API fails
        
        # Base allocation
        base_allocation = balance * Config.BANKROLL_FRACTION
        
        # Kelly fraction: f = edge / odds
        # Simplified: scale by edge
        kelly_multiplier = min(edge / 0.02, 2.0)  # Cap at 2x base
        
        allocation = base_allocation * kelly_multiplier
        
        # Hard cap
        max_allocation = balance * Config.BANKROLL_FRACTION_HARD_CAP
        allocation = min(allocation, max_allocation)
        
        # Convert to contracts
        cost_per_contract = entry_price  # in cents
        quantity = int(allocation / cost_per_contract)
        
        return max(quantity, 1)  # Minimum 1 contract
    
    def execute_entry(self, opportunity: EntryOpportunity, market_id: str, spot_price: float):
        """Execute entry trade"""
        
        logger.info(
            f"[ENTER] {market_id} {opportunity.direction} @ {opportunity.entry_price}¢ "
            f"edge={opportunity.edge:.3f} qty={opportunity.quantity}"
        )
        
        try:
            side = "yes" if opportunity.direction == "YES" else "no"
            
            order = self.client.create_order(
                market_ticker=market_id,
                side=side,
                action="buy",
                count=opportunity.quantity,
                price=opportunity.entry_price,
                order_type="limit" if Config.POST_ONLY else "market"
            )
            
            if not Config.DRY_RUN:
                logger.info(f"[ENTER] Order ID: {order.get('order', {}).get('order_id')}")
            
            # Create position
            self.position = Position(
                market_id=market_id,
                direction=opportunity.direction,
                quantity=opportunity.quantity,
                entry_price_cents=opportunity.entry_price,
                entry_time=time.time(),
                entry_model_prob=opportunity.model_prob,
                entry_market_prob=opportunity.market_prob,
                entry_spot_price=spot_price
            )
            
            self.state = State.HOLDING
            
            # Log trade
            trade_entry = {
                "market": market_id,
                "direction": opportunity.direction,
                "entry_price": opportunity.entry_price,
                "quantity": opportunity.quantity,
                "edge": opportunity.edge,
                "model_prob": opportunity.model_prob,
                "market_prob": opportunity.market_prob,
                "entry_time": time.time()
            }
            self.trade_log.append(trade_entry)
            
        except Exception as e:
            logger.error(f"[ENTER] Error executing entry: {e}")
    
    def run_market_loop(self, market: dict):
        """Main loop for single market"""
        
        self.market_id = market["ticker"]
        self.state = State.IDLE
        
        logger.info(f"[START] {self.market_id}")
        
        last_log_time = 0
        
        while True:
            try:
                # Get time to close
                t_close = self.get_time_to_close(market)
                
                if t_close < 0:
                    logger.info(f"[SETTLED] {self.market_id}")
                    self.state = State.SETTLED
                    break
                
                # Get Bitcoin spot price
                spot_price = BitcoinPriceFeed.get_spot_price()
                if not spot_price:
                    time.sleep(2)
                    continue
                
                # Get threshold
                threshold = self.parse_market_threshold(market)
                if not threshold:
                    logger.error(f"[ERROR] Could not parse threshold for {self.market_id}")
                    time.sleep(5)
                    continue
                
                # Calculate sigma
                sigma = ProbabilityModel.calculate_sigma(t_close)
                
                # Calculate model probability (for YES/above)
                p_model = ProbabilityModel.calculate_probability(
                    spot_price=spot_price,
                    threshold=threshold,
                    time_to_close_sec=t_close,
                    direction="above"
                )
                
                # Get orderbook
                orderbook = self.get_orderbook_snapshot(self.market_id)
                p_market = orderbook.midpoint_probability()
                
                # Blend probabilities
                p_blend = ProbabilityModel.blend_probabilities(p_model, p_market)
                
                # Log periodically
                now = time.time()
                if now - last_log_time > 10:
                    logger.info(
                        f"[STATE] {self.market_id} {self.state.value} t_close={t_close}s "
                        f"spot=${spot_price:.2f} p_model={p_model:.3f} p_market={p_market:.3f}"
                    )
                    last_log_time = now
                
                # STATE MACHINE
                
                if self.state == State.HOLDING and self.position:
                    # Check dump conditions
                    dump_decision = self.check_dump_conditions(
                        position=self.position,
                        p_model=p_blend,
                        p_market=p_market,
                        spot_price=spot_price,
                        t_close=t_close,
                        threshold=threshold,
                        sigma=sigma
                    )
                    
                    if dump_decision.should_dump:
                        self.execute_dump(self.position, dump_decision.reason, orderbook)
                    
                elif self.state in [State.IDLE, State.ARMED]:
                    # Check if we should arm
                    if t_close <= Config.ARM_TIME_SEC and t_close > Config.STOP_NEW_ENTRY_SEC:
                        self.state = State.ARMED
                        
                        # Check if we should enter
                        if not self.position or not Config.ONE_TRADE_PER_MARKET:
                            opportunity = self.evaluate_entry(
                                p_model=p_blend,
                                p_market=p_market,
                                orderbook=orderbook,
                                t_close=t_close,
                                spot_price=spot_price,
                                threshold=threshold
                            )
                            
                            if opportunity.should_trade:
                                self.execute_entry(opportunity, self.market_id, spot_price)
                    
                    elif t_close < Config.STOP_NEW_ENTRY_SEC:
                        if not self.position:
                            logger.info(f"[SKIP] {self.market_id} missed entry window")
                            self.state = State.SKIP
                
                # Sleep between iterations
                time.sleep(1)
                
            except KeyboardInterrupt:
                logger.info("[STOP] Interrupted by user")
                break
            except Exception as e:
                logger.error(f"[ERROR] Loop error: {e}", exc_info=True)
                time.sleep(5)

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point"""
    
    logger.warning("="*80)
    logger.warning("KALSHI BITCOIN TRADING BOT - AGGRESSIVE STRATEGY")
    logger.warning("="*80)
    logger.warning(f"DRY_RUN: {Config.DRY_RUN}")
    logger.warning(f"ENABLE_TRADING: {Config.ENABLE_TRADING}")
    logger.warning(f"SERIES: {Config.SERIES}")
    logger.warning(f"ARM_TIME: {Config.ARM_TIME_SEC}s")
    logger.warning(f"PROB_MIN: {Config.PROB_MIN}")
    logger.warning(f"EDGE_MIN: {Config.EDGE_MIN}")
    logger.warning(f"ENABLE_DUMP: {Config.ENABLE_DUMP}")
    logger.warning(f"DUMP_PROB_FLIP: {Config.DUMP_PROB_FLIP}")
    logger.warning("="*80)
    
    if not Config.ENABLE_TRADING:
        logger.error("Trading disabled. Set ENABLE_TRADING=True")
        return
    
    # Initialize client
    client = KalshiClient()
    
    # Test connection
    try:
        status = client.get_exchange_status()
        logger.info(f"[API] Exchange status: {status.get('trading_active')}")
        
        balance = client.get_balance()
        logger.info(f"[API] Balance: ${balance/100:.2f}")
    except Exception as e:
        logger.error(f"[API] Connection failed: {e}")
        return
    
    # Get markets
    if Config.MARKET_OVERRIDE:
        markets = [client.get_market(Config.MARKET_OVERRIDE)]
        logger.info(f"[MARKET] Override: {Config.MARKET_OVERRIDE}")
    else:
        markets = client.get_markets(Config.SERIES, status="open")
        logger.info(f"[MARKET] Found {len(markets)} open markets in {Config.SERIES}")
    
    if not markets:
        logger.error("[MARKET] No markets found")
        return
    
    # Trade each market sequentially
    engine = TradingEngine(client)
    
    for market in markets:
        logger.info(f"[NEXT] Trading {market['ticker']}")
        engine.run_market_loop(market)
        
        # Brief pause between markets
        time.sleep(2)
    
    # Summary
    logger.warning("="*80)
    logger.warning("SESSION SUMMARY")
    logger.warning(f"Trades entered: {len(engine.trade_log)}")
    logger.warning(f"Positions dumped: {len(engine.dump_log)}")
    logger.warning("="*80)
    
    # Print dump log
    if engine.dump_log:
        logger.warning("DUMP LOG:")
        for dump in engine.dump_log:
            logger.warning(
                f"  {dump['market']} {dump['direction']} "
                f"P&L: ${dump['pnl_dollars']:.2f} Reason: {dump['reason']}"
            )

if __name__ == "__main__":
    main()
