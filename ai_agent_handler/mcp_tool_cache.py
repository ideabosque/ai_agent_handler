#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Tool Cache Module

This module provides caching functionality for MCP tool lists,
reducing redundant tool discovery calls and improving initialization performance.
"""

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CacheEntry:
    """Cache entry for MCP tools."""
    tools: List[Any]
    tools_for_llm: List[Dict[str, Any]]
    timestamp: float
    tool_names: List[str] = field(default_factory=list)


class MCPToolCache:
    """MCP Tool Cache Manager.
    
    Provides caching for MCP tool lists with TTL-based expiration.
    Thread-safe implementation for concurrent access with read-heavy optimization.
    """
    
    def __init__(
        self,
        ttl_seconds: int = 600,
        max_size: int = 200,
        logger: Optional[logging.Logger] = None
    ):
        """Initialize the MCP tool cache.
        
        Args:
            ttl_seconds: Time-to-live for cache entries in seconds (default: 600 = 10 minutes)
            max_size: Maximum number of cache entries
            logger: Logger instance
        """
        self.ttl = ttl_seconds
        self.max_size = max_size
        self.logger = logger or logging.getLogger(__name__)
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "total_requests": 0
        }
    
    def _generate_cache_key(self, mcp_server_config: Dict[str, Any]) -> str:
        """Generate a unique cache key for an MCP server configuration.
        
        Args:
            mcp_server_config: MCP server configuration dictionary
            
        Returns:
            MD5 hash string as cache key
        """
        # Extract relevant configuration for key generation
        key_data = {
            "name": mcp_server_config.get("name", ""),
            "setting": mcp_server_config.get("setting", {})
        }
        
        # Sort keys for consistent hashing
        config_str = json.dumps(key_data, sort_keys=True)
        return hashlib.md5(config_str.encode()).hexdigest()
    
    def get(
        self,
        mcp_server_config: Dict[str, Any]
    ) -> Optional[Tuple[List[Any], List[Dict[str, Any]], List[str]]]:
        """Get cached tools for an MCP server configuration.
        
        Args:
            mcp_server_config: MCP server configuration dictionary
            
        Returns:
            Tuple of (tools, tools_for_llm, tool_names) if cached and valid, None otherwise
        """
        self._stats["total_requests"] += 1
        
        cache_key = self._generate_cache_key(mcp_server_config)
        entry = self._cache.get(cache_key)
        
        if entry is None:
            self._stats["misses"] += 1
            self.logger.debug(
                f"Cache miss for MCP server: {mcp_server_config.get('name', 'unknown')}"
            )
            return None
        
        if time.time() - entry.timestamp > self.ttl:
            with self._lock:
                if cache_key in self._cache and time.time() - self._cache[cache_key].timestamp > self.ttl:
                    del self._cache[cache_key]
            self._stats["misses"] += 1
            self.logger.debug(
                f"Cache expired for MCP server: {mcp_server_config.get('name', 'unknown')}"
            )
            return None
        
        self._stats["hits"] += 1
        self.logger.debug(
            f"Cache hit for MCP server: {mcp_server_config.get('name', 'unknown')} "
            f"(age: {time.time() - entry.timestamp:.1f}s)"
        )
        
        return (entry.tools, entry.tools_for_llm, entry.tool_names)
    
    def set(
        self,
        mcp_server_config: Dict[str, Any],
        tools: List[Any],
        tools_for_llm: List[Dict[str, Any]],
        tool_names: List[str]
    ) -> None:
        """Cache tools for an MCP server configuration.
        
        Args:
            mcp_server_config: MCP server configuration dictionary
            tools: List of MCP tool objects
            tools_for_llm: List of tools formatted for LLM
            tool_names: List of tool names
        """
        cache_key = self._generate_cache_key(mcp_server_config)
        
        with self._lock:
            if len(self._cache) >= self.max_size and cache_key not in self._cache:
                self._evict_oldest()
            
            self._cache[cache_key] = CacheEntry(
                tools=tools,
                tools_for_llm=tools_for_llm,
                timestamp=time.time(),
                tool_names=tool_names
            )
            
            self.logger.debug(
                f"Cached tools for MCP server: {mcp_server_config.get('name', 'unknown')} "
                f"({len(tool_names)} tools)"
            )
    
    def _evict_oldest(self) -> None:
        """Evict the oldest cache entry."""
        if not self._cache:
            return
        
        oldest_key = min(
            self._cache.keys(),
            key=lambda k: self._cache[k].timestamp
        )
        
        del self._cache[oldest_key]
        self._stats["evictions"] += 1
        
        self.logger.debug("Evicted oldest cache entry")
    
    def invalidate(self, mcp_server_config: Dict[str, Any]) -> bool:
        """Invalidate cache for a specific MCP server configuration.
        
        Args:
            mcp_server_config: MCP server configuration dictionary
            
        Returns:
            True if entry was invalidated, False if not found
        """
        with self._lock:
            cache_key = self._generate_cache_key(mcp_server_config)
            
            if cache_key in self._cache:
                del self._cache[cache_key]
                self.logger.debug(
                    f"Invalidated cache for MCP server: {mcp_server_config.get('name', 'unknown')}"
                )
                return True
            
            return False
    
    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self.logger.info(f"Cleared all {count} cache entries")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics.
        
        Returns:
            Dictionary containing cache statistics
        """
        with self._lock:
            total = self._stats["total_requests"]
            hit_rate = self._stats["hits"] / total if total > 0 else 0.0
            
            return {
                "total_requests": total,
                "hits": self._stats["hits"],
                "misses": self._stats["misses"],
                "evictions": self._stats["evictions"],
                "hit_rate": hit_rate,
                "cache_size": len(self._cache),
                "max_size": self.max_size,
                "ttl_seconds": self.ttl
            }
    
    def refresh_if_needed(
        self,
        mcp_server_config: Dict[str, Any],
        fetch_func: callable
    ) -> Tuple[List[Any], List[Dict[str, Any]], List[str]]:
        """Get cached tools or fetch if not cached/expired.
        
        This is a convenience method that handles cache miss automatically.
        
        Args:
            mcp_server_config: MCP server configuration dictionary
            fetch_func: Async function to fetch tools if not cached
                       Should return (tools, tools_for_llm, tool_names)
            
        Returns:
            Tuple of (tools, tools_for_llm, tool_names)
        """
        cached = self.get(mcp_server_config)
        
        if cached is not None:
            return cached
        
        # Fetch fresh data
        tools, tools_for_llm, tool_names = fetch_func()
        
        # Cache the result
        self.set(mcp_server_config, tools, tools_for_llm, tool_names)
        
        return (tools, tools_for_llm, tool_names)


# Global cache instance
_mcp_tool_cache: Optional[MCPToolCache] = None
_cache_lock = threading.Lock()


def get_mcp_tool_cache(
    ttl_seconds: int = 600,
    max_size: int = 200,
    logger: Optional[logging.Logger] = None
) -> MCPToolCache:
    """Get or create the global MCP tool cache instance.
    
    Args:
        ttl_seconds: Time-to-live for cache entries (default: 600 = 10 minutes)
        max_size: Maximum number of cache entries (default: 200)
        logger: Logger instance
        
    Returns:
        MCPToolCache instance
    """
    global _mcp_tool_cache
    
    with _cache_lock:
        if _mcp_tool_cache is None:
            _mcp_tool_cache = MCPToolCache(
                ttl_seconds=ttl_seconds,
                max_size=max_size,
                logger=logger
            )
        
        return _mcp_tool_cache


def reset_mcp_tool_cache() -> None:
    """Reset the global MCP tool cache instance.
    
    This should be called when configuration changes or for testing.
    """
    global _mcp_tool_cache
    
    with _cache_lock:
        if _mcp_tool_cache is not None:
            _mcp_tool_cache.clear()
            _mcp_tool_cache = None
