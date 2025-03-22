#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

import json
import logging
import os
import sys
import traceback
import zipfile
from typing import Any, Callable, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, NoCredentialsError

from silvaengine_utility import Utility


class AIAgentEventHandler:
    def __init__(
        self,
        logger: logging.Logger,
        agent: Dict[str, Any],
        **setting: Dict[str, Any],
    ):
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
            self.schemas = {}
            self.setting = setting

            # Will hold partial text from streaming
            self.accumulated_text: str = ""
            # Will hold the final output message data, if any
            self.final_output: Dict[str, Any] = {}

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

    def invoke_async_funct(self, function_name, **params: Dict[str, Any]) -> None:
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
        )

    def module_exists(self, module_name: str) -> bool:
        """Check if the module exists in the specified path."""
        module_dir = os.path.join(self.funct_extract_path, module_name)
        if os.path.exists(module_dir) and os.path.isdir(module_dir):
            self.logger.info(
                f"Module {module_name} found in {self.funct_extract_path}."
            )
            return True
        self.logger.info(
            f"Module {module_name} not found in {self.funct_extract_path}."
        )
        return False

    def download_and_extract_module(self, module_name: str) -> None:
        """Download and extract the module from S3 if not already extracted."""
        key = f"{module_name}.zip"
        zip_path = f"{self.funct_zip_path}/{key}"

        self.logger.info(
            f"Downloading module from S3: bucket={self.funct_bucket_name}, key={key}"
        )
        self.aws_s3.download_file(self.funct_bucket_name, key, zip_path)
        self.logger.info(f"Downloaded {key} from S3 to {zip_path}")

        # Extract the ZIP file
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(self.funct_extract_path)
        self.logger.info(f"Extracted module to {self.funct_extract_path}")

    def get_function(self, function_name: str) -> Optional[Callable]:
        try:
            function = self.agent["functions"].get(function_name)

            # Check if the module exists
            if not self.module_exists(function["module_name"]):
                # Download and extract the module if it doesn't exist
                self.download_and_extract_module(function["module_name"])

            # Add the extracted module to sys.path
            module_path = f"{self.funct_extract_path}/{function['module_name']}"
            if module_path not in sys.path:
                sys.path.append(module_path)

            assistant_function_class = getattr(
                __import__(function["module_name"]),
                function["class_name"],
            )

            configuration = self.agent.get("function_configuration", {})
            configuration.update(function.get("configuration", {}))
            return getattr(
                assistant_function_class(
                    self.logger,
                    **Utility.json_loads(Utility.json_dumps(configuration)),
                ),
                function_name,
            )
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
            self.send_data_to_websocket(
                data_format=data_format,
                chunk_delta=accumulated_partial_json,
                is_message_end=False,
            )
            complete_accumulated_json += (
                accumulated_partial_json  # Update complete JSON
            )
            accumulated_partial_json = ""  # Reset the partial JSON accumulator

        return complete_accumulated_json, accumulated_partial_json

    def send_data_to_websocket(
        self,
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
                "chunk_delta": chunk_delta,
                "is_message_end": is_message_end,
            }
        )

        Utility.invoke_funct_on_aws_lambda(
            self.logger,
            self._endpoint_id,
            "send_data_to_websocket",
            params={
                "connection_id": self._connection_id,
                "data": data,
            },
            message_group_id=message_group_id,
            setting=self.setting,
            test_mode=self.setting.get("test_mode"),
            aws_lambda=self.aws_lambda,
            invocation_type="Event",
            task_queue=self._task_queue,
        )
        return
