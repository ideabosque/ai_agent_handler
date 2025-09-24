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
from silvaengine_utility import Utility


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
            self._setup_function_paths(setting)

            self.logger = logger
            self.agent = agent
            self._endpoint_id = None
            self._run = None
            self._connection_id = None
            self._task_queue = None
            self._short_term_memory = []
            self.setting = setting

            self.mcp_http_clients = []
            if "mcp_servers" in self.agent:
                if self.agent["llm"]["llm_name"] in ["gemini", "claude"]:
                    self._initialize_mcp_http_clients(logger, self.agent["mcp_servers"])
                elif self.agent["llm"]["llm_name"] == "openai":
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
    def endpoint_id(self) -> str:
        return self._endpoint_id

    @endpoint_id.setter
    def endpoint_id(self, value: str) -> None:
        self._endpoint_id = value

    @property
    def run(self) -> Dict[str, Any]:
        return self._run

    @run.setter
    def run(self, value: Dict[str, Any]) -> None:
        self._run = value

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @connection_id.setter
    def connection_id(self, value: str) -> None:
        self._connection_id = value

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

    def _setup_function_paths(self, setting: Dict[str, Any]) -> None:
        self.funct_bucket_name = setting.get("funct_bucket_name")
        self.funct_zip_path = setting.get("funct_zip_path", "/tmp/funct_zips")
        self.funct_extract_path = setting.get("funct_extract_path", "/tmp/functs")
        os.makedirs(self.funct_zip_path, exist_ok=True)
        os.makedirs(self.funct_extract_path, exist_ok=True)

    def _initialize_mcp_http_clients(
        self, logger: logging.Logger, mcp_servers: List[Dict[str, Any]]
    ):
        for mcp_server in mcp_servers:
            mcp_http_client = MCPHttpClient(logger, **mcp_server["setting"])
            tools = asyncio.run(self._run_list_mcp_http_tools(mcp_http_client))
            tools_for_llm = mcp_http_client.export_tools_for_llm(
                self.agent["llm"]["llm_name"], tools
            )
            if "tools" in self.agent["configuration"]:
                self.agent["configuration"]["tools"].extend(tools_for_llm)
            else:
                self.agent["configuration"]["tools"] = tools_for_llm
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
                "thread_uuid": self._run["thread"]["thread_uuid"],
                "run_uuid": self._run["run_uuid"],
                "updated_by": self._run["updated_by"],
            }
        )
        Utility.invoke_funct_on_aws_lambda(
            self.logger,
            self._endpoint_id,
            function_name,
            params=params,
            setting=self.setting,
            test_mode=self.setting.get("test_mode"),
            aws_lambda=self.aws_lambda,
            invocation_type="Event",
            message_group_id=self._run["run_uuid"],
            task_queue=self._task_queue,
        )

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

            raise Exception(f"Function {function_name} not found in MCP tools")
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
    ) -> str:
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
        self, index: int, accumulated_partial_text: str, output_format: str
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
                        )
                        index += 1
                else:
                    self.send_data_to_stream(
                        index=index,
                        data_format=output_format,
                        chunk_delta=accumulated_partial_text,
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
    ) -> None:
        """
        Send data to WebSocket connection.

        Args:
            data_format (str): Format of the data being sent (default: "text")
            chunk_delta (str): Data chunk to be sent (default: "")
            is_message_end (bool): Flag indicating if this is the last message (default: False)
        """
        if self._connection_id is None or self._run is None:
            return

        message_group_id = f"{self._connection_id}-{self._run['run_uuid']}"

        data = Utility.json_dumps(
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
            self._endpoint_id,
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
        return
