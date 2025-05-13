# 🧠 AI Agent Event Handlers

This module provides a unified and extensible interface for interacting with multiple large language model (LLM) providers, enabling consistent and modular event handling for AI-driven applications. It follows an object-oriented design to support **OpenAI**, **Gemini**, **Anthropic**, and **Ollama** models, all under a shared abstraction layer: `AIAgentEventHandler`.

## 📐 Architecture Overview
![AI Agent Event Handler Class Diagram](/images/ai_agent_event_handler_class_diagram.jpg)

At the core of the system is the `AIAgentEventHandler` base class, which defines shared logic and interface methods for invoking model functions, streaming outputs, and handling responses.

Four concrete handler classes inherit from this base:

* **`OpenAIEventHandler`**: Interfaces with OpenAI's models.
* **`GeminiEventHandler`**: Connects to Google's Gemini API through the `genai.Client`.
* **`AnthropicEventHandler`**: Manages interactions with Anthropic's Claude models.
* **`OllamaEventHandler`**: Handles local LLM inference with Ollama and supports tool usage.

Each subclass is responsible for:

* Managing model-specific clients and configuration.
* Implementing `invoke_model()` and optionally `stream_response()` methods.
* Managing session memory, threading, and streaming tokens.

## 🧩 Key Features

* ✅ **Unified API for LLM invocation**
* 🔁 **Support for both standard and streaming responses**
* 🔌 **Tool usage and system prompts for advanced interactions (Ollama)**
* 🧠 **Assistant memory support for context-aware interactions**
* 📊 **Compatible with modular function routing and observability layers**

## 🔧 Primary Use Cases

This module is designed for applications that orchestrate LLMs across different providers—such as chatbots, agent frameworks, RAG pipelines, or AI-driven workflows—while maintaining a consistent interface and centralized control.
