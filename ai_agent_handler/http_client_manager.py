#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP Client Manager Module

This module provides a centralized HTTP client management system with
connection pooling, HTTP/2 support, and connection reuse for improved
performance across all provider handlers.
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class HTTPClientManager:
    """Centralized HTTP Client Manager with connection pooling.
    
    This class implements a singleton pattern to manage HTTP clients
    across all provider handlers, ensuring connection reuse and
    optimal resource utilization.
    
    Features:
    - HTTP/2 support for improved performance
    - Connection pooling with configurable limits
    - Keep-alive connections for reduced latency
    - Thread-safe client management
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """Singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(
        self,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        keepalive_expiry: float = 60.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        write_timeout: float = 60.0,
        logger: Optional[logging.Logger] = None
    ):
        """Initialize the HTTP Client Manager.
        
        Args:
            max_connections: Maximum number of connections in the pool
            max_keepalive_connections: Maximum number of keep-alive connections
            keepalive_expiry: Time in seconds before idle connections are closed
            connect_timeout: Timeout for establishing connections
            read_timeout: Timeout for reading data
            write_timeout: Timeout for writing data
            logger: Logger instance
        """
        if self._initialized:
            return
        
        self.logger = logger or logging.getLogger(__name__)
        
        # Get configuration from environment variables
        self.max_connections = int(
            os.getenv("HTTP_MAX_CONNECTIONS", str(max_connections))
        )
        self.max_keepalive_connections = int(
            os.getenv("HTTP_MAX_KEEPALIVE_CONNECTIONS", str(max_keepalive_connections))
        )
        self.keepalive_expiry = float(
            os.getenv("HTTP_KEEPALIVE_EXPIRY", str(keepalive_expiry))
        )
        self.connect_timeout = float(
            os.getenv("HTTP_CONNECT_TIMEOUT", str(connect_timeout))
        )
        self.read_timeout = float(
            os.getenv("HTTP_READ_TIMEOUT", str(read_timeout))
        )
        self.write_timeout = float(
            os.getenv("HTTP_WRITE_TIMEOUT", str(write_timeout))
        )
        
        # Client storage
        self._http_clients: Dict[str, httpx.Client] = {}
        self._async_http_clients: Dict[str, httpx.AsyncClient] = {}
        self._client_lock = threading.Lock()
        
        self._initialized = True
        
        self.logger.info(
            f"HTTPClientManager initialized with max_connections={self.max_connections}, "
            f"max_keepalive={self.max_keepalive_connections}, keepalive_expiry={self.keepalive_expiry}s"
        )
    
    def get_client(
        self,
        provider: str,
        base_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        http2: bool = True
    ) -> httpx.Client:
        """Get or create a synchronous HTTP client for a provider.
        
        Args:
            provider: Provider name (e.g., "openai", "anthropic", "gemini")
            base_url: Optional base URL for the client
            headers: Optional default headers
            http2: Whether to enable HTTP/2 support
            
        Returns:
            Configured httpx.Client instance
        """
        if not HTTPX_AVAILABLE:
            raise ImportError("httpx is not installed")
        
        cache_key = f"{provider}_{base_url or 'default'}"
        
        with self._client_lock:
            if cache_key not in self._http_clients:
                self.logger.info(f"Creating new HTTP client for provider: {provider}")
                
                limits = httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_keepalive_connections,
                    keepalive_expiry=self.keepalive_expiry,
                )
                
                timeout = httpx.Timeout(
                    connect=self.connect_timeout,
                    read=self.read_timeout,
                    write=self.write_timeout,
                )
                
                self._http_clients[cache_key] = httpx.Client(
                    base_url=base_url,
                    headers=headers,
                    limits=limits,
                    timeout=timeout,
                    http2=http2,
                )
                
                self.logger.debug(f"Created HTTP client for {provider} with HTTP/2={http2}")
            
            return self._http_clients[cache_key]
    
    def get_async_client(
        self,
        provider: str,
        base_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        http2: bool = True
    ) -> httpx.AsyncClient:
        """Get or create an asynchronous HTTP client for a provider.
        
        Args:
            provider: Provider name (e.g., "openai", "anthropic", "gemini")
            base_url: Optional base URL for the client
            headers: Optional default headers
            http2: Whether to enable HTTP/2 support
            
        Returns:
            Configured httpx.AsyncClient instance
        """
        if not HTTPX_AVAILABLE:
            raise ImportError("httpx is not installed")
        
        cache_key = f"{provider}_{base_url or 'default'}"
        
        with self._client_lock:
            if cache_key not in self._async_http_clients:
                self.logger.info(f"Creating new async HTTP client for provider: {provider}")
                
                limits = httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_keepalive_connections,
                    keepalive_expiry=self.keepalive_expiry,
                )
                
                timeout = httpx.Timeout(
                    connect=self.connect_timeout,
                    read=self.read_timeout,
                    write=self.write_timeout,
                )
                
                self._async_http_clients[cache_key] = httpx.AsyncClient(
                    base_url=base_url,
                    headers=headers,
                    limits=limits,
                    timeout=timeout,
                    http2=http2,
                )
                
                self.logger.debug(f"Created async HTTP client for {provider} with HTTP/2={http2}")
            
            return self._async_http_clients[cache_key]
    
    def get_openai_client(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        organization: Optional[str] = None
    ) -> "openai.OpenAI":
        """Get or create an OpenAI client with optimized HTTP settings.
        
        Args:
            api_key: OpenAI API key
            base_url: Optional base URL for the API
            organization: Optional organization ID
            
        Returns:
            Configured OpenAI client instance
        """
        if not OPENAI_AVAILABLE:
            raise ImportError("openai is not installed")
        
        cache_key = f"openai_{organization or 'default'}"
        
        with self._client_lock:
            if cache_key not in self._http_clients:
                self.logger.info("Creating new OpenAI client with optimized HTTP settings")
                
                # Create optimized httpx client
                http_client = self.get_client(
                    provider="openai",
                    base_url=base_url,
                    http2=True
                )
                
                self._http_clients[cache_key] = openai.OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    organization=organization,
                    http_client=http_client,
                )
                
                self.logger.debug("Created OpenAI client with HTTP/2 connection pooling")
            
            return self._http_clients[cache_key]
    
    def close_client(self, provider: str, base_url: Optional[str] = None) -> None:
        """Close and remove a specific client.
        
        Args:
            provider: Provider name
            base_url: Optional base URL used when creating the client
        """
        cache_key = f"{provider}_{base_url or 'default'}"
        
        with self._client_lock:
            if cache_key in self._http_clients:
                self._http_clients[cache_key].close()
                del self._http_clients[cache_key]
                self.logger.info(f"Closed HTTP client for {provider}")
            
            if cache_key in self._async_http_clients:
                # Async clients should be closed in async context
                del self._async_http_clients[cache_key]
                self.logger.info(f"Removed async HTTP client for {provider}")
    
    def close_all(self) -> None:
        """Close all HTTP clients and release resources."""
        with self._client_lock:
            for client in self._http_clients.values():
                try:
                    client.close()
                except Exception as e:
                    self.logger.error(f"Error closing HTTP client: {e}")
            
            self._http_clients.clear()
            self._async_http_clients.clear()
            
            self.logger.info("Closed all HTTP clients")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about managed clients.
        
        Returns:
            Dictionary containing client statistics
        """
        with self._client_lock:
            return {
                "sync_clients": len(self._http_clients),
                "async_clients": len(self._async_http_clients),
                "max_connections": self.max_connections,
                "max_keepalive_connections": self.max_keepalive_connections,
                "keepalive_expiry": self.keepalive_expiry,
                "providers": list(set(
                    k.split("_")[0] for k in self._http_clients.keys()
                ))
            }


# Global instance
_http_client_manager: Optional[HTTPClientManager] = None
_manager_lock = threading.Lock()


def get_http_client_manager(
    max_connections: int = 100,
    max_keepalive_connections: int = 20,
    keepalive_expiry: float = 60.0,
    logger: Optional[logging.Logger] = None
) -> HTTPClientManager:
    """Get or create the global HTTP Client Manager instance.
    
    Args:
        max_connections: Maximum number of connections in the pool
        max_keepalive_connections: Maximum number of keep-alive connections
        keepalive_expiry: Time in seconds before idle connections are closed
        logger: Logger instance
        
    Returns:
        HTTPClientManager instance
    """
    global _http_client_manager
    
    with _manager_lock:
        if _http_client_manager is None:
            _http_client_manager = HTTPClientManager(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
                keepalive_expiry=keepalive_expiry,
                logger=logger
            )
        
        return _http_client_manager


def reset_http_client_manager() -> None:
    """Reset the global HTTP Client Manager instance.
    
    This should be called when configuration changes or for testing.
    """
    global _http_client_manager
    
    with _manager_lock:
        if _http_client_manager is not None:
            _http_client_manager.close_all()
            _http_client_manager = None
