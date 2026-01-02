#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

import asyncio
import json
import logging
import os
import sys
import traceback
import zipfile
from typing import Any, Callable, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, NoCredentialsError

from mcp_http_client import MCPHttpClient
from silvaengine_utility.invoker import Invoker
from silvaengine_utility.serializer import Serializer


class AIAgentEventHandler:
    def __init__(
        self,
        logger: logging.Logger,
        agent: Dict[str, Any],
        **setting: Dict[str, Any],
    ) -> None:
        """
        Initialize the OpenAIFunctBase class.
        :param logger: Logger instance for logging errors and information.
        :param setting: Configuration setting for AWS credentials and region.
        """
        try:
            self._initialize_aws_services(setting)

            self.logger = logger
            self.agent = agent
            self._context = {}
            self._run = None
            self._task_queue = None
            self._short_term_memory = []
            self.setting = setting

            self.mcp_http_clients = []
            if "mcp_servers" in self.agent:
                if self.agent["configuration"].pop("mcp_llm_native", False):
                    tools = [
                        {
                            "type": "mcp",
                            "server_label": mcp_server["name"],
                            "server_url": mcp_server["setting"]["base_url"],
                            "headers": mcp_server["setting"]["headers"],
                            "require_approval": "never",
                        }
                        for mcp_server in self.agent["mcp_servers"]
                    ]
                    if "tools" in self.agent["configuration"]:
                        self.agent["configuration"]["tools"].extend(tools)
                    else:
                        self.agent["configuration"]["tools"] = tools
                else:
                    if self.agent["llm"]["llm_name"] in [
                        "gemini",
                        "claude",
                        "gpt",
                        "ollama",
                        "travrse",
                    ]:
                        self._initialize_mcp_http_clients(
                            logger, self.agent["mcp_servers"]
                        )
                    else:
                        raise Exception(
                            f"Unsupported LLM name: {self.agent['llm']['llm_name']}"
                        )

            # Will hold partial text from streaming
            self.accumulated_text: str = ""
            # Will hold the final output message data, if any
            self.final_output: Dict[str, Any] = {}
            self.uploaded_files: List[Dict[str, Any]] = []

        except (BotoCoreError, NoCredentialsError) as boto_error:
            self.logger.error(f"AWS Boto3 error: {boto_error}")
            raise boto_error
        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            raise e

    @property
    def context(self) -> Dict[str, Any]:
        return self._context

    @context.setter
    def context(self, value: Dict[str, Any]) -> None:
        self._context = value

    @property
    def run(self) -> Dict[str, Any] | None:
        return self._run

    @run.setter
    def run(self, value: Dict[str, Any]) -> None:
        self._run = value

    @property
    def task_queue(self) -> object:
        return self._task_queue

    @task_queue.setter
    def task_queue(self, value: object) -> None:
        self._task_queue = value

    @property
    def short_term_memory(self) -> object:
        return self._short_term_memory

    @short_term_memory.setter
    def short_term_memory(self, value: object) -> None:
        self._short_term_memory = value

    def _initialize_aws_services(self, setting: Dict[str, Any]) -> None:
        if all(
            setting.get(k)
            for k in ["region_name", "aws_access_key_id", "aws_secret_access_key"]
        ):
            aws_credentials = {
                "region_name": setting["region_name"],
                "aws_access_key_id": setting["aws_access_key_id"],
                "aws_secret_access_key": setting["aws_secret_access_key"],
            }
        else:
            aws_credentials = {}

        self.aws_lambda = boto3.client("lambda", **aws_credentials)
        self.aws_s3 = boto3.client("s3", **aws_credentials)

    def _initialize_mcp_http_clients(
        self, logger: logging.Logger, mcp_servers: List[Dict[str, Any]]
    ):
        if "tools" not in self.agent["configuration"]:
            self.agent["configuration"]["tools"] = []

        for mcp_server in mcp_servers:
            mcp_http_client = MCPHttpClient(logger, **mcp_server["setting"])
            tools = asyncio.run(self._run_list_mcp_http_tools(mcp_http_client))
            tools_for_llm = mcp_http_client.export_tools_for_llm(
                self.agent["llm"]["llm_name"], tools
            )

            self.agent["configuration"]["tools"].extend(tools_for_llm)

            self.mcp_http_clients.append(
                {
                    "name": mcp_server["name"],
                    "client": mcp_http_client,
                    "tools": [tool.name for tool in tools],
                }
            )

    async def _run_list_mcp_http_tools(self, mcp_http_client):
        async with mcp_http_client as client:
            return await client.list_tools()

    async def _run_call_mcp_http_tool(self, mcp_http_client, name, arguments):
        self.logger.info(f"Calling MCP HTTP tool: {name} with arguments: {arguments}")

        async with mcp_http_client as client:
            result = await client.call_tool(name, arguments)

            self.logger.info(f"MCP HTTP tool {name} returned result: {result}")

            return result

    def invoke_async_funct(self, function_name: str, **params: Dict[str, Any]) -> None:
        if self._run is None:
            return

        params.update(
            {
                "thread_uuid": self._run["thread_uuid"],
                "run_uuid": self._run["run_uuid"],
                "updated_by": self._run["updated_by"],
            }
        )
        Invoker.invoke_funct_on_aws_lambda(
            self._context,
            function_name,
            params=params,
            aws_lambda=self.aws_lambda,
            invocation_type="Event",
            message_group_id=self._run["run_uuid"],
            task_queue=self._task_queue,
        )

    def _module_exists(self, module_name: str) -> bool:
        """Check if the module exists in the specified path."""
        funct_extract_path = self.setting.get("funct_extract_path", "")

        module_dir = os.path.join(
            self.setting.get("funct_extract_path", ""), module_name
        )
        if os.path.exists(module_dir) and os.path.isdir(module_dir):
            self.logger.info(f"Module {module_name} found in {funct_extract_path}.")
            return True
        self.logger.info(f"Module {module_name} not found in {funct_extract_path}.")
        return False

    def _download_and_extract_package(self, package_name: str) -> None:
        """Download and extract the module from S3 if not already extracted."""
        funct_bucket_name = self.setting.get("funct_bucket_name", "")
        funct_zip_path = self.setting.get("funct_zip_path", "")
        funct_extract_path = self.setting.get("funct_extract_path", "")

        key = f"{package_name}.zip"
        zip_path = f"{funct_zip_path}/{key}"

        self.logger.info(
            f"Downloading module from S3: bucket={funct_bucket_name}, key={key}"
        )
        self.aws_s3.download_file(funct_bucket_name, key, zip_path)
        self.logger.info(f"Downloaded {key} from S3 to {zip_path}")

        # Extract the ZIP file
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(funct_extract_path)
        self.logger.info(f"Extracted module to {funct_extract_path}")

    def _get_module(
        self, package_name: str, module_name: str, source: str = None
    ) -> type:
        try:
            """Get the module class from the package."""
            funct_extract_path = self.setting.get("funct_extract_path", "")

            if source is None:
                return getattr(__import__(module_name), module_name)

            # Check if the module exists
            if not self._module_exists(module_name):
                # Download and extract the module if it doesn't exist
                self._download_and_extract_package(package_name)

            # Add the extracted module to sys.path
            module_path = f"{funct_extract_path}"
            if module_path not in sys.path:
                sys.path.append(module_path)

            # Import the module and get the class
            module = __import__(module_name)
            return module
        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            raise e

    def _get_class(
        self, package_name: str, module_name: str, class_name: str, source: str = None
    ) -> Any:
        try:
            # Import the module and get the class
            module = self._get_module(package_name, module_name, source=source)
            return getattr(module, class_name)
        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            raise e

    def _find_tool_config(
        self, function_name: str
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Find the tool module and class configuration for a given function name.

        Args:
            function_name: The name of the function to find

        Returns:
            A tuple of (tool_module_config, tool_class_config)

        Raises:
            Exception: If the function is not found in any tool module or class
        """
        tool_modules = self.agent.get("tool_modules", [])

        if not tool_modules:
            raise Exception(
                f"Function '{function_name}' not found: No tool_modules configured in agent"
            )

        # Search for the module containing the function
        for tool_module in tool_modules:
            tool_classes = tool_module.get("tool_classes", [])

            for tool_class in tool_classes:
                tool_functions = tool_class.get("tool_functions", [])

                if function_name in tool_functions:
                    # Validate required fields
                    if "module_name" not in tool_module:
                        raise Exception(
                            f"Invalid tool_module configuration: 'module_name' is required"
                        )
                    if "class_name" not in tool_class:
                        raise Exception(
                            f"Invalid tool_class configuration: 'class_name' is required"
                        )

                    return tool_module, tool_class

        # Function not found in any module
        available_functions = [
            func
            for module in tool_modules
            for cls in module.get("tool_classes", [])
            for func in cls.get("tool_functions", [])
        ]

        raise Exception(
            f"Function '{function_name}' not found in tool_modules. "
            f"Available functions: {', '.join(available_functions) if available_functions else 'none'}"
        )

    def _instantiate_tool(
        self,
        tool_class: type,
        tool_module_config: Dict[str, Any],
        tool_class_config: Dict[str, Any],
    ) -> Any:
        """
        Instantiate a tool class with its configuration.

        Args:
            tool_class: The tool class to instantiate
            tool_module_config: Module configuration containing settings
            tool_class_config: Class configuration containing setting_name reference

        Returns:
            An instance of the tool class
        """
        # Get the setting_name from the class config
        setting_name = tool_class_config.get("setting_name")

        # Retrieve the corresponding settings from the module config
        if setting_name:
            module_settings = tool_module_config.get("settings", {})
            configuration = module_settings.get(setting_name, {})
        else:
            # Fallback to empty configuration if no setting_name is specified
            configuration = {}

        # Normalize and pass the configuration to the tool class
        normalized_config = Serializer.json_normalize(configuration)

        return tool_class(self.logger, **normalized_config)

    def _set_context_attributes(self, tool_instance: Any) -> None:
        """
        Set endpoint_id and part_id attributes on the tool instance if supported.

        Args:
            tool_instance: The tool instance to set attributes on
        """
        if hasattr(tool_instance, "endpoint_id") and hasattr(tool_instance, "part_id"):
            tool_instance.endpoint_id = self.context.get("endpoint_id")
            tool_instance.part_id = self.context.get("part_id")

    def get_function(self, function_name: str) -> Optional[Callable]:
        try:
            # Find the MCP HTTP client that has the requested function name in its available tools
            mcp_http_client = next(
                (
                    client["client"]
                    for client in self.mcp_http_clients
                    if function_name in client["tools"]
                ),
                None,
            )

            # If function is found in MCP tools, return a lambda that calls it through the MCP client
            if mcp_http_client:
                return lambda **arguments: asyncio.run(
                    self._run_call_mcp_http_tool(
                        mcp_http_client, function_name, arguments
                    )
                )

            # Expected tool_modules data structure:
            # A list of tool module configurations. Each module can define reusable settings
            # and contain one or more tool classes that reference those settings.
            #
            # Example:
            # [
            #     {
            #         "module_name": "my_tools_module",           # Required: Python module name to import
            #         "package_name": "my_tools_package",         # Optional: Package name for S3 download
            #         "source": "s3",                             # Optional: Source location ("s3", "local", etc.)
            #         "settings": {                               # Optional: Shared settings dictionary
            #             "db_config": {                          # Setting name (referenced by tool classes)
            #                 "host": "localhost",
            #                 "port": 5432,
            #                 "database": "mydb"
            #             },
            #             "api_config": {                         # Another setting configuration
            #                 "base_url": "https://api.example.com",
            #                 "timeout": 30
            #             }
            #         },
            #         "tool_classes": [                           # Required: List of tool classes in this module
            #             {
            #                 "class_name": "DatabaseTool",       # Required: Class name to instantiate
            #                 "setting_name": "db_config",        # Optional: References settings["db_config"]
            #                 "tool_functions": ["query", "insert"] # Required: Available function names
            #             },
            #             {
            #                 "class_name": "ApiTool",
            #                 "setting_name": "api_config",       # References settings["api_config"]
            #                 "tool_functions": ["get", "post"]
            #             }
            #         ]
            #     }
            # ]
            #
            # Flow:
            # 1. Find function in tool_classes -> tool_functions list
            # 2. Get setting_name from the matching tool_class
            # 3. Lookup configuration from module's settings[setting_name]
            # 4. Instantiate class with: ClassName(logger, **configuration)

            # Find the tool module and class configuration that contains the requested function
            tool_module_config, tool_class_config = self._find_tool_config(
                function_name
            )

            # Dynamically load the tool class from the module
            tool_class = self._get_class(
                tool_module_config.get("package_name"),
                tool_module_config["module_name"],
                tool_class_config["class_name"],
                source=tool_module_config.get("source"),
            )

            # Instantiate the tool with its configuration
            tool_instance = self._instantiate_tool(
                tool_class, tool_module_config, tool_class_config
            )

            # Set endpoint and part IDs if the tool supports them
            self._set_context_attributes(tool_instance)

            return getattr(tool_instance, function_name)

        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            raise e

    # Utility Function: Validate and Complete JSON
    def try_complete_json(self, accumulated: str) -> str:
        """
        Try to validate and complete the JSON by adding various combinations of closing brackets.

        Args:
            accumulated (str): The accumulated JSON string that may be incomplete.

        Returns:
            str: The ending string that successfully completes the JSON, or an empty string if no valid completion is found.
        """
        try:
            json.loads(accumulated)  # Attempt to parse with the accumulated
            return accumulated
        except json.JSONDecodeError:
            possible_endings = ["}", "]", "}", "}]", "}]}", "}}"]
            for ending in possible_endings:
                try:
                    completed_json = accumulated + ending
                    json.loads(
                        completed_json
                    )  # Attempt to parse with the potential ending
                    return ending  # If parsing succeeds, return the successful ending
                except json.JSONDecodeError:
                    continue
        return ""  # If no ending works, return empty string

    # Helper Function: Process and Send JSON
    def process_and_send_json(
        self,
        index: int,
        complete_accumulated_json: str,
        accumulated_partial_json: str,
        data_format: str,
    ) -> tuple[int, str, str]:
        """
        Process and send JSON if it forms a valid structure.

        Args:
            complete_accumulated_json (str): The complete JSON string for validation.
            accumulated_partial_json (str): The accumulated partial JSON string.
            data_format (str): The format of the data being sent.

        Returns:
            tuple: A tuple containing:
                - str: The updated complete JSON string after sending the partial JSON
                - str: The updated accumulated partial JSON string
        """
        combined_json = complete_accumulated_json + accumulated_partial_json
        ending = self.try_complete_json(combined_json)

        if ending:
            # Send the accumulated partial JSON to WebSocket Service
            self.send_data_to_stream(
                index=index,
                data_format=data_format,
                chunk_delta=accumulated_partial_json,
                is_message_end=False,
            )
            complete_accumulated_json += (
                accumulated_partial_json  # Update complete JSON
            )
            accumulated_partial_json = ""  # Reset the partial JSON accumulator
            index += 1

        return index, complete_accumulated_json, accumulated_partial_json

    def process_text_content(
        self,
        index: int,
        accumulated_partial_text: str,
        output_format: str,
        suffix: str = "",
    ) -> tuple[int, str]:
        """
        Process XML content from accumulated text and send to stream.

        Args:
            accumulated_partial_text: Text buffer containing potential XML content
            index: Current stream index
            output_format: Current output format

        Returns:
            Tuple of (updated index, remaining text buffer)
        """

        # Check if text contains XML-style tags and update format
        if "<" in accumulated_partial_text:
            output_format = "xml"

            # Wait for closing tag before sending, with max buffer size check
            if ">" in accumulated_partial_text or len(accumulated_partial_text) > 500:
                # If no closing tag found within buffer limit, treat as regular text
                if (
                    ">" not in accumulated_partial_text
                    and len(accumulated_partial_text) > 500
                ):
                    output_format = "text"

                if output_format == "xml":
                    # Extract XML tags and text content
                    xml_parts = []
                    current_text = accumulated_partial_text
                    while "<" in current_text:
                        start_idx = current_text.find("<")
                        if start_idx > 0:
                            xml_parts.append(current_text[:start_idx])
                        end_idx = current_text.find(">", start_idx)
                        if end_idx == -1:
                            xml_parts.append(current_text[start_idx:])
                            current_text = ""
                            break
                        xml_parts.append(current_text[start_idx : end_idx + 1])
                        current_text = current_text[end_idx + 1 :]
                    if current_text:
                        xml_parts.append(current_text)

                    for part in xml_parts:
                        self.send_data_to_stream(
                            index=index,
                            data_format=(
                                output_format if "<" in part and ">" in part else "text"
                            ),
                            chunk_delta=part,
                            suffix=suffix,
                        )
                        index += 1
                else:
                    self.send_data_to_stream(
                        index=index,
                        data_format=output_format,
                        chunk_delta=accumulated_partial_text,
                        suffix=suffix,
                    )
                    index += 1
                accumulated_partial_text = (
                    ""  # For non-XML content, use buffer threshold
                )
        elif len(accumulated_partial_text) >= int(
            self.setting.get("accumulated_partial_text_buffer", "10")
        ):
            self.send_data_to_stream(
                index=index,
                data_format=output_format,
                chunk_delta=accumulated_partial_text,
                suffix=suffix,
            )
            accumulated_partial_text = ""
            index += 1
        return index, accumulated_partial_text

    def send_data_to_stream(
        self,
        index: int = 0,
        data_format: str = "text",
        chunk_delta: str = "",
        is_message_end: bool = False,
        suffix: str = "",
    ) -> None:
        """
        Send data to WebSocket connection.

        Args:
            data_format (str): Format of the data being sent (default: "text")
            chunk_delta (str): Data chunk to be sent (default: "")
            is_message_end (bool): Flag indicating if this is the last message (default: False)
        """
        connection_id = self._context.get("connection_id")
        if connection_id is None or self._run is None:
            return

        message_group_id = f"{connection_id}-{self._run['run_uuid']}"
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

        Invoker.invoke_funct_on_aws_lambda(
            self._context,
            "send_data_to_stream",
            params={
                "connection_id": self._context.get("connection_id"),
                "data": data,
            },
            aws_lambda=self.aws_lambda,
            invocation_type="Event",
            message_group_id=message_group_id,
            task_queue=self._task_queue,
        )
        return
