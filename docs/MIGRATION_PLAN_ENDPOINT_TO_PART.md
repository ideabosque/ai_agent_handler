# Migration Plan: endpoint_id → endpoint_id + part_id (AI Agent Handler)

> **Migration Status**: Planning Phase
> **Last Updated**: 2025-12-11
> **Target Completion**: TBD
> **Risk Level**: Low (Minimal Breaking Change)

---

## Executive Summary

### What's Changing?

Migrate the `AIAgentEventHandler` class from single `endpoint_id` to dual `endpoint_id` + `part_id` pattern to align with the `ai_agent_core_engine` migration.

**Current State:**
```python
ai_agent_handler.endpoint_id = "acme-corp-prod"  # Single identifier
```

**Target State:**
```python
ai_agent_handler.endpoint_id = "aws-prod-us-east-1"  # Platform identifier
ai_agent_handler.part_id = "acme-corp"               # Business partition
# partition_key assembled internally: "aws-prod-us-east-1#acme-corp"
```

### Key Principles

**Internal Partition Key Assembly:**
- Add new `part_id` property alongside existing `endpoint_id`
- Assemble `partition_key = f"{endpoint_id}#{part_id}"` internally when calling AWS Lambda
- Use assembled `partition_key` for all AWS Lambda database operations
- Keep `endpoint_id` available for backward compatibility and non-DB operations (logging, external APIs)

**Minimal Breaking Changes:**
- No changes to public API signatures
- Only internal Lambda invocation calls updated
- Handlers receive both `endpoint_id` and `part_id` from `ai_agent_core_engine`

**Backward Compatibility:**
- If `part_id` not provided, use `endpoint_id` as the partition identifier
- Existing code continues to work during migration period

---

## 1. Module Overview

### 1.1 AIAgentEventHandler Class

**File:** `ai_agent_handler/ai_agent_handler.py`

**Purpose:** Base class for handling AI agent interactions across multiple LLM providers (OpenAI, Gemini, Anthropic, Ollama, Travrse).

**Key Responsibilities:**
- Initialize AWS services (Lambda, S3)
- Initialize MCP HTTP clients for tool usage
- Invoke async Lambda functions for database operations
- Stream data to WebSocket connections via Lambda
- Process text, XML, and JSON content
- Manage short-term memory and run context

**Concrete Handler Classes (inherit from AIAgentEventHandler):**
- `OpenAIEventHandler` - OpenAI models
- `GeminiEventHandler` - Google Gemini API
- `AnthropicEventHandler` - Anthropic Claude models
- `OllamaEventHandler` - Local Ollama inference
- `TravrseEventHandler` - Travrse integration

---

## 2. Current endpoint_id Usage

### 2.1 Property Definition

**Lines 91-97:**
```python
@property
def endpoint_id(self) -> str | None:
    return self._endpoint_id

@endpoint_id.setter
def endpoint_id(self, value: str) -> None:
    self._endpoint_id = value
```

**Initialization:** Set to `None` in `__init__()` (Line 37)

### 2.2 Usage Locations

The `endpoint_id` is used in **2 critical locations** for AWS Lambda database operations:

#### Location 1: invoke_async_funct() (Lines 200-222)

**Purpose:** Asynchronously invoke Lambda functions for database operations

**Current Code:**
```python
def invoke_async_funct(self, function_name: str, **params: Dict[str, Any]) -> None:
    if self._run is None:
        return

    params.update(
        {
            "thread_uuid": self._run["thread"]["thread_uuid"],
            "run_uuid": self._run["run_uuid"],
            "updated_by": self._run["updated_by"],
        }
    )
    Utility.invoke_funct_on_aws_lambda(
        self.logger,
        self._endpoint_id,              # <-- Used here for Lambda targeting
        function_name,
        params=params,
        setting=self.setting,
        test_mode=self.setting.get("test_mode"),
        aws_lambda=self.aws_lambda,
        invocation_type="Event",
        message_group_id=self._run["run_uuid"],
        task_queue=self._task_queue,
    )
```

**What it does:**
- Invokes Lambda functions asynchronously (e.g., create_run, update_thread, etc.)
- Uses `endpoint_id` to identify target Lambda endpoint
- Passes run context (thread_uuid, run_uuid, updated_by)
- Messages grouped by run_uuid in task queue

#### Location 2: send_data_to_stream() (Lines 401-449)

**Purpose:** Send streamed data chunks to WebSocket connections via Lambda

**Current Code:**
```python
def send_data_to_stream(
    self,
    index: int = 0,
    data_format: str = "text",
    chunk_delta: str = "",
    is_message_end: bool = False,
    suffix: str = "",
) -> None:
    if self._connection_id is None or self._run is None:
        return

    message_group_id = f"{self._connection_id}-{self._run['run_uuid']}"
    if suffix:
        message_group_id += f"-{suffix}"

    data = Serializer.json_dumps(
        {
            "message_group_id": message_group_id,
            "data_format": data_format,
            "index": index,
            "chunk_delta": chunk_delta,
            "is_message_end": is_message_end,
        }
    )

    Utility.invoke_funct_on_aws_lambda(
        self.logger,
        self._endpoint_id,              # <-- Used here for Lambda targeting
        "send_data_to_stream",
        params={
            "connection_id": self._connection_id,
            "data": data,
        },
        setting=self.setting,
        test_mode=self.setting.get("test_mode"),
        aws_lambda=self.aws_lambda,
        invocation_type="Event",
        message_group_id=message_group_id,
        task_queue=self._task_queue,
    )
```

**What it does:**
- Sends streamed text/XML/JSON chunks to WebSocket
- Uses `endpoint_id` to target Lambda function
- Groups messages by connection_id + run_uuid (+ optional suffix)
- Maintains order via index parameter

---

## 3. Migration Changes

### 3.1 Add part_id Property

**File:** `ai_agent_handler/ai_agent_handler.py`

**Location:** After `endpoint_id` property (Lines 91-97)

**New Code:**
```python
@property
def endpoint_id(self) -> str | None:
    return self._endpoint_id

@endpoint_id.setter
def endpoint_id(self, value: str) -> None:
    self._endpoint_id = value

# NEW: Add part_id property
@property
def part_id(self) -> str | None:
    return self._part_id

@part_id.setter
def part_id(self, value: str) -> None:
    self._part_id = value
```

**Initialization Change (Line 37):**

**Before:**
```python
self._endpoint_id = None
self._run = None
self._connection_id = None
self._task_queue = None
self._short_term_memory = []
```

**After:**
```python
self._endpoint_id = None
self._part_id = None  # NEW
self._run = None
self._connection_id = None
self._task_queue = None
self._short_term_memory = []
```

### 3.2 Update invoke_async_funct() Method

**File:** `ai_agent_handler/ai_agent_handler.py`

**Location:** Lines 200-222

**Current Code:**
```python
def invoke_async_funct(self, function_name: str, **params: Dict[str, Any]) -> None:
    if self._run is None:
        return

    params.update(
        {
            "thread_uuid": self._run["thread"]["thread_uuid"],
            "run_uuid": self._run["run_uuid"],
            "updated_by": self._run["updated_by"],
        }
    )
    Utility.invoke_funct_on_aws_lambda(
        self.logger,
        self._endpoint_id,              # OLD
        function_name,
        params=params,
        setting=self.setting,
        test_mode=self.setting.get("test_mode"),
        aws_lambda=self.aws_lambda,
        invocation_type="Event",
        message_group_id=self._run["run_uuid"],
        task_queue=self._task_queue,
    )
```

**New Code:**
```python
def invoke_async_funct(self, function_name: str, **params: Dict[str, Any]) -> None:
    if self._run is None:
        return

    params.update(
        {
            "thread_uuid": self._run["thread"]["thread_uuid"],
            "run_uuid": self._run["run_uuid"],
            "updated_by": self._run["updated_by"],
        }
    )

    # Assemble partition_key from endpoint_id and part_id
    # If part_id not provided, fallback to endpoint_id for backward compatibility
    if self._part_id:
        partition_key = f"{self._endpoint_id}#{self._part_id}"
    else:
        partition_key = self._endpoint_id

    Utility.invoke_funct_on_aws_lambda(
        self.logger,
        partition_key,                  # CHANGED: Use assembled partition_key for DB operations
        function_name,
        params=params,
        setting=self.setting,
        test_mode=self.setting.get("test_mode"),
        aws_lambda=self.aws_lambda,
        invocation_type="Event",
        message_group_id=self._run["run_uuid"],
        task_queue=self._task_queue,
    )
```

**Changes:**
- Assemble `partition_key` from `endpoint_id` and `part_id` internally
- If `part_id` not provided, use `endpoint_id` alone for backward compatibility
- Lambda functions will receive composite partition_key for database operations

### 3.3 Update send_data_to_stream() Method

**File:** `ai_agent_handler/ai_agent_handler.py`

**Location:** Lines 401-449

**Current Code:**
```python
def send_data_to_stream(
    self,
    index: int = 0,
    data_format: str = "text",
    chunk_delta: str = "",
    is_message_end: bool = False,
    suffix: str = "",
) -> None:
    if self._connection_id is None or self._run is None:
        return

    message_group_id = f"{self._connection_id}-{self._run['run_uuid']}"
    if suffix:
        message_group_id += f"-{suffix}"

    data = Serializer.json_dumps(
        {
            "message_group_id": message_group_id,
            "data_format": data_format,
            "index": index,
            "chunk_delta": chunk_delta,
            "is_message_end": is_message_end,
        }
    )

    Utility.invoke_funct_on_aws_lambda(
        self.logger,
        self._endpoint_id,              # OLD
        "send_data_to_stream",
        params={
            "connection_id": self._connection_id,
            "data": data,
        },
        setting=self.setting,
        test_mode=self.setting.get("test_mode"),
        aws_lambda=self.aws_lambda,
        invocation_type="Event",
        message_group_id=message_group_id,
        task_queue=self._task_queue,
    )
```

**New Code:**
```python
def send_data_to_stream(
    self,
    index: int = 0,
    data_format: str = "text",
    chunk_delta: str = "",
    is_message_end: bool = False,
    suffix: str = "",
) -> None:
    if self._connection_id is None or self._run is None:
        return

    message_group_id = f"{self._connection_id}-{self._run['run_uuid']}"
    if suffix:
        message_group_id += f"-{suffix}"

    data = Serializer.json_dumps(
        {
            "message_group_id": message_group_id,
            "data_format": data_format,
            "index": index,
            "chunk_delta": chunk_delta,
            "is_message_end": is_message_end,
        }
    )

    # Assemble partition_key from endpoint_id and part_id
    # If part_id not provided, fallback to endpoint_id for backward compatibility
    if self._part_id:
        partition_key = f"{self._endpoint_id}#{self._part_id}"
    else:
        partition_key = self._endpoint_id

    Utility.invoke_funct_on_aws_lambda(
        self.logger,
        partition_key,                  # CHANGED: Use assembled partition_key for DB operations
        "send_data_to_stream",
        params={
            "connection_id": self._connection_id,
            "data": data,
        },
        setting=self.setting,
        test_mode=self.setting.get("test_mode"),
        aws_lambda=self.aws_lambda,
        invocation_type="Event",
        message_group_id=message_group_id,
        task_queue=self._task_queue,
    )
```

**Changes:**
- Assemble `partition_key` from `endpoint_id` and `part_id` internally
- If `part_id` not provided, use `endpoint_id` alone for backward compatibility
- Ensures streaming continues to work during migration
- Lambda receives correct partition identifier for database writes

---

## 4. Integration with ai_agent_core_engine

### 4.1 Handler Instantiation Changes

**File:** `ai_agent_core_engine/handlers/ai_agent.py` (not in this repo, but documented for reference)

**Before:**
```python
ai_agent_handler = ai_agent_handler_class(
    info.context.get("logger"),
    agent.__dict__,
    **info.context.get("setting", {}),
)
ai_agent_handler.endpoint_id = endpoint_id
ai_agent_handler.run = run.__dict__
ai_agent_handler.connection_id = connection_id
```

**After:**
```python
# Extract both endpoint_id and part_id from context
endpoint_id = info.context.get("endpoint_id")
part_id = info.context.get("part_id")  # NEW

ai_agent_handler = ai_agent_handler_class(
    info.context.get("logger"),
    agent.__dict__,
    **info.context.get("setting", {}),
)
ai_agent_handler.endpoint_id = endpoint_id  # Platform identifier
ai_agent_handler.part_id = part_id          # NEW: Business partition
ai_agent_handler.run = run.__dict__
ai_agent_handler.connection_id = connection_id
```

**Changes:**
- Extract both `endpoint_id` and `part_id` from GraphQL context
- Set both on handler instance
- Handler will assemble `partition_key` internally when needed for Lambda calls
- `endpoint_id` remains available for logging, external APIs, non-DB operations

### 4.2 Data Flow

```
User Request
    ↓
ai_agent_core_engine/main.py
    - Extract endpoint_id and part_id from params
    - Assemble partition_key = f"{endpoint_id}#{part_id}" (for core engine DB operations)
    - Add endpoint_id, part_id, and partition_key to GraphQL context
    ↓
ai_agent_core_engine/handlers/ai_agent.py
    - Extract endpoint_id and part_id from context
    - Instantiate AIAgentEventHandler
    - Set handler.endpoint_id = endpoint_id
    - Set handler.part_id = part_id (NEW)
    ↓
AIAgentEventHandler (this module)
    - Use endpoint_id for logging, external APIs
    - Assemble partition_key = f"{endpoint_id}#{part_id}" when needed
    - Use partition_key for Lambda invocations (DB operations)
    ↓
Utility.invoke_funct_on_aws_lambda(partition_key, ...)
    ↓
AWS Lambda Function
    - Receives partition_key
    - Performs database operations with composite key
```

---

## 5. Usage Pattern

### 5.1 Dual Property Pattern with Internal Assembly

The handler will have access to both `endpoint_id` and `part_id`, and will assemble `partition_key` internally:

```python
class AIAgentEventHandler:
    def __init__(self, logger, agent, **setting):
        self.logger = logger
        self.agent = agent
        # Set after instantiation:
        self._endpoint_id = None      # Platform identifier
        self._part_id = None          # Business partition
        self._run = None
        self._connection_id = None
        self._task_queue = None

    def invoke_async_funct(self, function_name: str, **params):
        """Assemble partition_key and use for database operations via Lambda."""
        # Assemble partition_key internally
        if self._part_id:
            partition_key = f"{self._endpoint_id}#{self._part_id}"
        else:
            partition_key = self._endpoint_id  # Backward compatibility

        Utility.invoke_funct_on_aws_lambda(
            self.logger,
            partition_key,  # Use assembled partition_key for DB operations
            function_name,
            params=params,
            # ...
        )

    def send_data_to_stream(self, **kwargs):
        """Assemble partition_key and use for stream data Lambda calls."""
        # Assemble partition_key internally
        if self._part_id:
            partition_key = f"{self._endpoint_id}#{self._part_id}"
        else:
            partition_key = self._endpoint_id  # Backward compatibility

        Utility.invoke_funct_on_aws_lambda(
            self.logger,
            partition_key,  # Use assembled partition_key for DB operations
            "send_data_to_stream",
            # ...
        )

    def log_activity(self):
        """Use endpoint_id for logging if needed."""
        self.logger.info(f"Processing request for endpoint {self._endpoint_id}")
```

**Key Points:**
- Handler receives `endpoint_id` and `part_id` separately from `ai_agent_core_engine`
- `partition_key` is assembled internally as needed: `f"{endpoint_id}#{part_id}"`
- Use assembled `partition_key` for all AWS Lambda database operations
- Use `endpoint_id` for logging, metrics, external APIs if needed
- Fallback to `endpoint_id` alone if `part_id` not set (backward compatibility)
- Both `endpoint_id` and `part_id` set by `ai_agent_core_engine` during handler instantiation

---

## 6. File Change Summary

### Files Requiring Changes

**Main Handler Class (1 file):**
- `ai_agent_handler/ai_agent_handler.py` - Add part_id property and update Lambda invocations with internal partition_key assembly

**Total: 1 file**

### Line Changes Summary

| Location | Lines | Change Type | Description |
|----------|-------|-------------|-------------|
| **Property Definition** | After 91-97 | Addition | Add part_id property with getter/setter |
| **Initialization** | Line 37 | Addition | Add `self._part_id = None` |
| **invoke_async_funct()** | Lines 200-222 | Modification | Assemble partition_key and use for Lambda invocation |
| **send_data_to_stream()** | Lines 401-449 | Modification | Assemble partition_key and use for Lambda invocation |

**Total Changes:** ~20 lines added/modified

---

## 7. Testing Strategy

### 7.1 Unit Tests

**Test part_id property:**
```python
def test_part_id_property():
    handler = AIAgentEventHandler(logger, agent_dict, **settings)

    # Test setter/getter
    handler.part_id = "acme-corp"
    assert handler.part_id == "acme-corp"

    # Test None initialization
    new_handler = AIAgentEventHandler(logger, agent_dict, **settings)
    assert new_handler.part_id is None
```

**Test backward compatibility (endpoint_id only):**
```python
def test_backward_compatibility_with_endpoint_id_only():
    handler = AIAgentEventHandler(logger, agent_dict, **settings)
    handler.endpoint_id = "aws-prod"
    handler.run = {"thread": {"thread_uuid": "thread-1"}, "run_uuid": "run-1", "updated_by": "user"}

    # Mock Lambda invocation
    with patch.object(Utility, 'invoke_funct_on_aws_lambda') as mock_invoke:
        handler.invoke_async_funct("test_function", param1="value1")

        # Should use endpoint_id when part_id is None
        call_args = mock_invoke.call_args
        assert call_args[0][1] == "aws-prod"  # endpoint_id used (no part_id)
```

**Test partition_key assembly with part_id:**
```python
def test_partition_key_assembly_in_lambda_calls():
    handler = AIAgentEventHandler(logger, agent_dict, **settings)
    handler.endpoint_id = "aws-prod"
    handler.part_id = "acme-corp"
    handler.run = {"thread": {"thread_uuid": "thread-1"}, "run_uuid": "run-1", "updated_by": "user"}

    # Mock Lambda invocation
    with patch.object(Utility, 'invoke_funct_on_aws_lambda') as mock_invoke:
        handler.invoke_async_funct("test_function", param1="value1")

        # Should assemble and use partition_key when part_id is available
        call_args = mock_invoke.call_args
        assert call_args[0][1] == "aws-prod#acme-corp"  # assembled partition_key used
```

### 7.2 Integration Tests

**Test handler instantiation from ai_agent_core_engine:**
```python
def test_handler_setup_with_part_id():
    # Simulate ai_agent_core_engine context
    context = {
        "logger": logger,
        "endpoint_id": "aws-prod",
        "part_id": "acme-corp",
        "setting": settings,
    }

    handler = AIAgentEventHandler(logger, agent_dict, **context["setting"])
    handler.endpoint_id = context["endpoint_id"]
    handler.part_id = context["part_id"]

    assert handler.endpoint_id == "aws-prod"
    assert handler.part_id == "acme-corp"
```

**Test streaming with assembled partition_key:**
```python
def test_send_data_to_stream_with_part_id():
    handler = AIAgentEventHandler(logger, agent_dict, **settings)
    handler.endpoint_id = "aws-prod"
    handler.part_id = "acme-corp"
    handler.connection_id = "conn-123"
    handler.run = {"run_uuid": "run-456"}

    with patch.object(Utility, 'invoke_funct_on_aws_lambda') as mock_invoke:
        handler.send_data_to_stream(
            index=0,
            data_format="text",
            chunk_delta="Hello world",
            is_message_end=False,
        )

        # Verify assembled partition_key used
        call_args = mock_invoke.call_args
        assert call_args[0][1] == "aws-prod#acme-corp"  # assembled partition_key
        assert call_args[0][2] == "send_data_to_stream"  # function name
```

### 7.3 Backward Compatibility Tests

**Test that existing code without part_id still works:**
```python
def test_legacy_code_compatibility():
    # Old code that only sets endpoint_id (no part_id)
    handler = AIAgentEventHandler(logger, agent_dict, **settings)
    handler.endpoint_id = "legacy-endpoint"
    handler.run = {"thread": {"thread_uuid": "t1"}, "run_uuid": "r1", "updated_by": "user"}

    with patch.object(Utility, 'invoke_funct_on_aws_lambda') as mock_invoke:
        handler.invoke_async_funct("legacy_function")

        # Should fall back to endpoint_id when part_id is None
        call_args = mock_invoke.call_args
        assert call_args[0][1] == "legacy-endpoint"
```

---

## 8. Migration Phases

### Phase 1: Code Changes (Week 1)
- [ ] Add `part_id` property to `AIAgentEventHandler`
- [ ] Update `invoke_async_funct()` to assemble partition_key with fallback
- [ ] Update `send_data_to_stream()` to assemble partition_key with fallback
- [ ] Add unit tests for new property and partition_key assembly logic

### Phase 2: Integration Testing (Week 2)
- [ ] Update `ai_agent_core_engine` to pass part_id to handlers
- [ ] Test handler instantiation with both endpoint_id and part_id
- [ ] Verify Lambda invocations receive correct assembled partition_key
- [ ] Test streaming with assembled partition_key

### Phase 3: Deployment (Week 3)
- [ ] Deploy to dev environment
- [ ] Verify backward compatibility with existing deployments
- [ ] Deploy to staging
- [ ] Monitor metrics and logs
- [ ] Deploy to production (staged rollout)

### Phase 4: Cleanup (Week 4)
- [ ] Remove fallback logic after all systems migrated
- [ ] Update documentation
- [ ] Archive migration plan

---

## 9. Risk Assessment

### Risk Level: **Low**

**Reasons:**
1. **Minimal Code Changes:** Only 1 file, ~15 lines modified
2. **Backward Compatible:** Falls back to endpoint_id if partition_key not set
3. **No API Breaking Changes:** Public interface unchanged
4. **Isolated Impact:** Changes only affect Lambda invocation internal calls
5. **Easy Rollback:** Can revert single file if issues occur

### Potential Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Fallback logic fails | Low | Medium | Comprehensive unit tests |
| Integration breaks | Low | High | Integration tests with ai_agent_core_engine |
| Lambda receives wrong key | Low | High | Test Lambda invocation mocking |
| Backward compatibility issue | Low | Medium | Test legacy code paths |

---

## 10. Rollback Plan

### Immediate Rollback
1. Revert `ai_agent_handler.py` to previous version
2. Restart services
3. Monitor logs and metrics
4. Notify stakeholders

### Partial Rollback
- Keep partition_key property but disable usage in Lambda calls
- Use feature flag to toggle between endpoint_id and partition_key

---

## 11. Dependencies

### Upstream Dependencies (Must Complete First)
- [ ] `ai_agent_core_engine` migration completed
- [ ] GraphQL context includes `endpoint_id` and `part_id`
- [ ] Lambda functions updated to handle composite partition_key format

### Downstream Dependencies (Blocked Until Complete)
- None (this is a leaf module in the dependency chain)

---

## 12. Success Criteria

- [ ] All unit tests pass
- [ ] Integration tests with ai_agent_core_engine pass
- [ ] Backward compatibility maintained (works without part_id)
- [ ] Lambda invocations use assembled partition_key when part_id is provided
- [ ] Fallback to endpoint_id works correctly when part_id is None
- [ ] No errors in production logs
- [ ] Metrics show normal operation

---

## Document Version

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-12-11 | Initial migration plan for ai_agent_handler module |
| 1.1 | 2025-12-11 | Updated to use part_id instead of partition_key - handler assembles partition_key internally |

---

**End of Document**
