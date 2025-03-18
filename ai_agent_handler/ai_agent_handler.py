#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

import logging
import os
import sys
import traceback
import zipfile
from typing import Any, Callable, Dict, Optional

import boto3
import humps
from botocore.exceptions import BotoCoreError, NoCredentialsError

from silvaengine_utility import Utility


class AIAgentEventHandler:
    def __init__(
        self,
        logger: logging.Logger,
        agent: Dict[str, Any],
        run: Dict[str, Any],
        **setting: Dict[str, Any],
    ):
        """
        Initialize the OpenAIFunctBase class.
        :param logger: Logger instance for logging errors and information.
        :param setting: Configuration setting for AWS credentials and region.
        """
        try:
            self.logger = logger
            self.endpoint_id = setting.get("endpoint_id")
            self.agent = agent
            self.run = run
            self.schemas = {}
            self.setting = setting
            self._initialize_aws_services(setting)
            self._setup_function_paths(setting)
        except (BotoCoreError, NoCredentialsError) as boto_error:
            self.logger.error(f"AWS Boto3 error: {boto_error}")
            raise boto_error
        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            raise e

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
        Utility.invoke_funct_on_aws_lambda(
            self.logger,
            self.endpoint_id,
            function_name,
            params=params,
            setting=self.setting,
            test_mode=self.setting.get("test_mode"),
            aws_lambda=self.aws_lambda,
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

    def send_data_to_websocket(
        self,
        connection_id: str,
        data: Dict[str, Any],
        message_group_id: str = None,
    ) -> None:
        """
        Send data to WebSocket connection.

        Args:
            logger: Logger instance for logging.
            endpoint_id: AWS Lambda endpoint identifier.
            connection_id: WebSocket connection ID.
            data: Data to be sent.
            message_group_id: Unique identifier for message grouping.
            setting: Configuration settings.
        """
        if connection_id:
            Utility.invoke_funct_on_aws_lambda(
                self.logger,
                self.endpoint_id,
                "send_data_to_websocket",
                params={
                    "connection_id": connection_id,
                    "data": data,
                },
                message_group_id=message_group_id,
                setting=self.setting,
                test_mode=self.setting.get("test_mode"),
                aws_lambda=self.aws_lambda,
            )
            return
        return
