"""
Intelligent Observation Mesh - Layer 1
Monitors 10 Base L2 pools with redundant RPC connections and multi-CEX price feeds.
"""
import asyncio
import time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import numpy as np

from web3 import Web3, AsyncWeb3
from web3.exceptions import BlockNotFound, TransactionNotFound
import ccxt
import ccxt.async_support as ccxt_async

from firebase_admin import firestore
import firebase_admin
from google.cloud.firestore_v1.base_query import FieldFilter

from config.settings import settings, RPCEndpoint, CEXConfig, PoolConfig

logger = structlog.get_logger(__name__)

class ObserverHealth(Enum):
    """Health status of observer components"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"

@dataclass
class PriceObservation:
    """Standardized price observation with metadata"""
    timestamp: float
    pair: str
    price: float
    source: str
    volume_24h: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    confidence: float = 1.0  # 0.0 to 1.0

@dataclass
class PoolState:
    """Current state of a liquidity pool"""
    pool_address: str
    token0_reserve: float
    token1_reserve: float
    price: float
    liquidity_usd: float
    last_tx_hash: Optional[str] = None
    last_update: float = time.time()
    deviation_score: float = 0.0

class RedundantWeb3Client:
    """Web3 client with automatic failover between RPC endpoints"""
    
    def __init__(self, endpoints: List[RPCEndpoint]):
        self.endpoints = sorted(endpoints, key=lambda x: x.priority)
        self.active_endpoint: Optional[RPCEndpoint] = None
        self.web3_instances: Dict[str, Web3] = {}
        self.health_status: Dict[str, ObserverHealth] = {}
        self._initialize_clients()
        
    def _initialize_clients(self) -> None:
        """Initialize Web3 instances for all endpoints"""
        for endpoint in self.endpoints:
            try:
                w3 = Web3(Web3.HTTPProvider(
                    endpoint.url,
                    request_kwargs={'timeout': endpoint.timeout_seconds}
                ))
                
                # Test connection
                if w3.is_connected():
                    self.web3_instances[endpoint.provider] = w3
                    self.health_status[endpoint.provider] = ObserverHealth.HEALTHY
                    logger.info(f"Connected to {endpoint.provider} at {endpoint.url[:50]}...")
                    
                    if not self.active_endpoint:
                        self.active_endpoint = endpoint
                else:
                    self.health_status[endpoint.provider] = ObserverHealth.OFFLINE
                    logger.warning(f"Failed to connect to {endpoint.provider}")
                    
            except Exception as e:
                self.health_status[endpoint.provider] = ObserverHealth.UNHEALTHY
                logger.error(f"Error initializing {endpoint.provider}: {e}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError))
    )
    def call_contract(self, contract_address: str, abi: List[Dict], 
                     function_name: str, args: List[Any] = None) -> Any:
        """Make contract call with automatic failover"""
        last_error = None
        
        for endpoint in self.endpoints:
            if self.health_status.get(endpoint.provider) != ObserverHealth.HEALTHY:
                continue
                
            try:
                w3 = self.web3_instances[endpoint.provider]
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(contract_address),
                    abi=abi
                )
                
                func = getattr(contract.functions, function_name)
                result = func(*(args or [])).call()
                return result
                
            except Exception as e:
                last_error = e
                self.health_status[endpoint.provider] = ObserverHealth.DEGRADED
                logger.warning(f"Call failed on {endpoint.provider}: {e}")
                continue
        
        # All endpoints failed
        error_msg = f"All RPC endpoints failed for {function_name}: {last_error}"
        logger.error(error_msg)
        raise ConnectionError(error_msg)
    
    def get_health_report(self) -> Dict[str, str]:
        """Generate health report for all endpoints"""
        return {provider: status.value for provider, status in self.health_status.items()}

class CEXPriceAggregator:
    """Aggregates prices from multiple CEX sources with outlier detection"""
    
    def __init__(self, cex_configs: List[CEXConfig]):
        self.cex_configs = [c for c in cex_configs if c.enabled]
        self.exchanges: Dict[str, ccxt.Exchange] = {}
        self._initialize_exchanges()
        
    def _initialize_exchanges(self) -> None:
        """Initialize CCXT exchange instances"""
        for config in self.cex_configs:
            try:
                api_key = os.getenv(config.api_key_env)
                api_secret = os.getenv(config.api_secret_env)
                
                if not api_key or not api_secret:
                    logger.warning(f"Missing API credentials for {config.name}")
                    continue
                
                # Initialize synchronous exchange for now
                exchange_class = getattr(ccxt, config.name)
                exchange = exchange_class({
                    'apiKey': api_key,
                    'secret': api_secret,
                    'enableRateLimit': True,
                    'timeout': 10000,
                })
                
                # Test connectivity
                exchange.fetch_status()
                self.exchanges[config.name] = exchange
                logger.info(f"Connected to {config.name} exchange")
                
            except Exception as e:
                logger.error(f"Failed to initialize {config.name}: {e}")
    
    def get_aggregated_price(self, symbol: str) -> PriceObservation:
        """
        Get aggregated price from all available exchanges with outlier rejection
        """
        prices = []
        volumes = []
        observations = []
        
        for name, exchange in self.exchanges.items():
            try:
                ticker = exchange.fetch_ticker(symbol)
                
                # Basic data validation
                if ticker['last'] is None or ticker['last'] <= 0:
                    continue
                    
                if ticker['bid'] is not None and ticker['ask'] is not None:
                    spread = (ticker['ask'] - ticker['bid']) / ticker['last']
                    if spread > 0.1:  # Reject excessive spreads
                        continue
                
                price = float(ticker['last'])
                volume = float(ticker['quoteVolume']) if ticker['quoteVolume'] else 0
                
                prices.append(price)
                volumes.append(volume)
                
                observations.append(PriceObservation(
                    timestamp=time.time(),
                    pair=symbol,
                    price=price,
                    source=name,
                    volume_24h=volume,
                    bid=float(ticker['bid']) if ticker['bid'] else None,
                    ask=float(ticker['ask']) if ticker['ask'] else None,
                    confidence=1.0
                ))
                
            except Exception as e:
                logger.warning(f"Failed to fetch {symbol} from {name}: {e}")
                continue
        
        if not observations:
            raise ValueError(f"No valid price data for {symbol}")
        
        # Remove outliers using IQR method
        if len(prices) >= 3:
            q1, q3 = np.percentile(prices, [25, 75])
            iqr = q3 - q1
            lower_bound = q1 - 1.5 * iqr
            upper_bound = q3 + 1.5 * iqr
            
            filtered_obs = [
                obs for obs in observations
                if lower_bound <= obs.price <= upper_bound
            ]
            
            if filtered_obs:
                observations = filtered_obs
                prices = [obs.price for obs in observations]
        
        # Volume-weighted average price
        total_volume = sum(volumes)
        if total_volume > 0:
            vwap = sum(p * v for p, v in zip(prices, volumes)) / total_volume
        else:
            vwap = sum(prices) / len(prices)
        
        # Use the observation with price closest to VWAP
        best_obs = min(observations, key=lambda x: abs(x.price - vwap))
        
        return PriceObservation(
            timestamp=time.time(),
            pair=symbol,
            price=vwap,
            source="aggregated",
            volume_24h=total_volume,
            bid=min(obs.bid for obs in observations if obs.bid),
            ask=max(obs.ask for obs in observations if obs.ask),
            confidence=len(observations) / len(self.exchanges)
        )

class RegionalObserver:
    """
    Regional observer node monitoring Base L2 pools and price feeds.
    Deploy instances in US, EU, APAC regions for redundancy.
    """
    
    def __init__(self, region: str, firestore_client: firestore.Client):
        self.region = region
        self.firestore = firestore_client
        self.logger = logger.bind(region=region)
        
        # Initialize clients
        self.web3_client = RedundantWeb3Client(settings.BASE_RPC_ENDPOINTS)
        self.price