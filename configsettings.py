"""
Configuration management for Operation Capillary Siphon.
Centralized settings with environment variable fallbacks.
"""
import os
from dataclasses import dataclass
from typing import List, Dict, Optional
from dotenv import load_dotenv
import structlog

load_dotenv()
logger = structlog.get_logger(__name__)

@dataclass
class RPCEndpoint:
    """Represents an RPC endpoint with health metrics"""
    url: str
    provider: str
    priority: int = 1
    max_retries: int = 3
    timeout_seconds: int = 10

@dataclass
class CEXConfig:
    """Centralized Exchange configuration"""
    name: str
    api_key_env: str
    api_secret_env: str
    enabled: bool = True
    weight: float = 0.33  # Weight in price calculation

@dataclass
class PoolConfig:
    """Base L2 pool configuration"""
    address: str
    token0: str
    token1: str
    dex: str
    min_liquidity_usd: float = 1000
    max_deviation_percent: float = 1.0

class SystemSettings:
    """Singleton system configuration"""
    
    # Firebase Configuration
    FIREBASE_CREDENTIALS_PATH: str = os.getenv(
        "FIREBASE_CREDENTIALS_PATH", 
        "credentials/firebase-service-account.json"
    )
    
    # RPC Endpoints (Fallback strategy)
    BASE_RPC_ENDPOINTS: List[RPCEndpoint] = [
        RPCEndpoint(
            url=os.getenv("ALCHEMY_BASE_URL", "https://base-mainnet.g.alchemy.com/v2/"),
            provider="alchemy",
            priority=1
        ),
        RPCEndpoint(
            url=os.getenv("QUICKNODE_BASE_URL", "https://base.quiknode.pro/"),
            provider="quicknode",
            priority=2
        ),
        RPCEndpoint(
            url="https://mainnet.base.org",
            provider="public",
            priority=3
        ),
    ]
    
    # CEX Configuration
    CEX_CONFIGS: List[CEXConfig] = [
        CEXConfig(
            name="binance",
            api_key_env="BINANCE_API_KEY",
            api_secret_env="BINANCE_API_SECRET"
        ),
        CEXConfig(
            name="coinbase",
            api_key_env="COINBASE_API_KEY",
            api_secret_env="COINBASE_API_SECRET"
        ),
        CEXConfig(
            name="kraken",
            api_key_env="KRAKEN_API_KEY",
            api_secret_env="KRAKEN_API_SECRET"
        ),
    ]
    
    # Target Pools (10 designated Base L2 pools)
    TARGET_POOLS: List[PoolConfig] = [
        PoolConfig(
            address="0x...",  # USDC/ETH Uniswap V3 pool
            token0="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
            token1="0x4200000000000000000000000000000000000006",  # WETH
            dex="uniswap_v3",
            min_liquidity_usd=50000
        ),
        # Add 9 more pools here
    ]
    
    # Performance Targets
    DAILY_ROI_TARGET: float = 0.05  # 5%
    MAX_DAILY_LOSS: float = 0.02    # 2% circuit breaker
    MIN_PROFIT_USD: float = 0.50    # Minimum profit threshold
    
    # Execution Parameters
    GAS_MULTIPLIER: float = 1.1
    MAX_SLIPPAGE_BPS: int = 50      # 0.5%
    PRIVATE_MEMPOOL_ENABLED: bool = True
    
    # ML Model Paths
    MODEL_DIR: str = "models/"
    PRICE_PREDICTION_MODEL: str = "price_rf_v1.pkl"
    GAS_PREDICTION_MODEL: str = "gas_gbm_v1.pkl"
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    ENABLE_FORENSIC_LOGGING: bool = True
    
    @classmethod
    def validate_configuration(cls) -> None:
        """Validate all configuration parameters"""
        errors = []
        
        # Check Firebase credentials
        if not os.path.exists(cls.FIREBASE_CREDENTIALS_PATH):
            errors.append(f"Firebase credentials not found at {cls.FIREBASE_CREDENTIALS_PATH}")
        
        # Check RPC endpoints
        for endpoint in cls.BASE_RPC_ENDPOINTS:
            if not endpoint.url.startswith("http"):
                errors.append(f"Invalid RPC URL: {endpoint.url}")
        
        # Check CEX API keys
        for cex in cls.CEX_CONFIGS:
            if cex.enabled and not os.getenv(cex.api_key_env):
                errors.append(f"Missing API key for {cex.name}: {cex.api_key_env}")
        
        if errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(errors)
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        logger.info("Configuration validated successfully")

# Global settings instance
settings = SystemSettings()