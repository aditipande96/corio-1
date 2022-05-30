#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.
#
"""Mix(Write, Read, Validate, Delete in percentage) object operation workload for io stability."""

import os.path
import random
from datetime import datetime, timedelta
from math import modf
from time import perf_counter_ns

from src.commons.constants import LATEST_LOG_PATH
from src.commons.constants import MIN_DURATION
from src.commons.utils.cluster_utils import ClusterServices
from src.commons.utils.corio_utils import get_master_details
from src.commons.utils.corio_utils import run_local_cmd
from src.libs.s3api.s3_bucket_ops import S3Bucket
from src.libs.s3api.s3_object_ops import S3Object
from src.libs.tools.s3bench import S3bench


class TestMixObjectOps(S3Bucket, S3Object):
    """S3 mix object operations class for executing given io stability workload."""

    def __init__(self, access_key: str, secret_key: str, endpoint_url: str, **kwargs) -> None:
        """
        s3 Mix object operations init class.

        :param access_key: access key.
        :param secret_key: secret key.
        :param endpoint_url: endpoint with http or https.
        :param test_id: Test ID string.
        :param use_ssl: To use secure connection.
        :param object_size: Object size to be used for bucket operation
        :param seed: Seed to be used for random data generator
        :param session: session name.
        :param duration: Duration timedelta object, if not given will run for 100 days.
        """
        super().__init__(access_key, secret_key, endpoint_url=endpoint_url, use_ssl=kwargs.get(
            "use_ssl"), test_id=f"{kwargs.get('test_id')}_mix_s3io_operations")
        random.seed(kwargs.get("seed"))
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint_url = endpoint_url
        if not S3bench.install_s3bench():
            raise Exception("s3bench tool is not installed.")
        if kwargs.get("duration"):
            self.finish_time = datetime.now() + kwargs.get("duration")
        else:
            self.finish_time = datetime.now() + timedelta(hours=int(100 * 24))
        self.initialize_variables(**kwargs)

    @classmethod
    def initialize_variables(cls, **kwargs):
        """
        Initialize the variables.

        # :param write_percentage: Percentage of data to fill storage.
        # :param read_percentage: Percentage of data to read from storage.
        # :param delete_percentage: percentage of data to delete from storage.
        # :param cleanup_percentage: Once write reached to percentage then delete all data.
        # :param total_storage_size: Total storage on cloud.
        """
        cls.object_prefix = "s3mix_object_ops_iter{}"
        cls.bucket_name = f"s3mix-bucket-{perf_counter_ns()}"
        cls.write_percentage = kwargs.get("write_percentage")
        cls.read_percentage = kwargs.get("read_percentage")
        cls.delete_percentage = kwargs.get("delete_percentage")
        cls.cleanup_percentage = kwargs.get("cleanup_percentage")
        cls.total_storage = kwargs.get("total_storage_size")
        cls.object_size = kwargs.get("object_size")
        cls.sessions = kwargs.get("sessions")
        cls.region = kwargs.get("region", "us-east-1")
        # If user input is zero then we will use hctl status to fetch storage details.
        if not cls.total_storage:
            host, user, password = get_master_details()
            cluster_obj = ClusterServices(host, user, password)
            status, resp = cluster_obj.check_cluster_storage()
            assert status, f"Failed to get storage details: {resp}"
            cls.total_storage = resp["total_capacity"]
        cls.iteration = 0
        cls.total_written_data = 0
        cls.storage_size_to_fill = int(cls.total_storage / 100 * cls.write_percentage) if \
            cls.write_percentage else cls.total_storage
        cls.storage_size_to_read = int(cls.total_storage / 100 * cls.read_percentage) if \
            cls.read_percentage else cls.total_storage
        cls.storage_size_to_delete = int(cls.total_storage / 100 * cls.delete_percentage) \
            if cls.delete_percentage else cls.total_storage
        cls.size_to_cleanup_all_data = int(cls.total_storage / 100 * cls.cleanup_percentage) if \
            cls.cleanup_percentage else int(cls.total_storage / 100 * 90)
        cls.s3_url = None
        cls.report_path = os.path.join(
            LATEST_LOG_PATH, f"{kwargs.get('test_id')}_mix_s3io_operations_report.log")
        cls.label = kwargs.get("test_id")

    # pylint: disable=broad-except
    def execute_mix_object_workload(self):
        """Execute mix object operations workload for specific duration."""
        # TODO: disable_background_delete/enable_background_delete
        self.create_s3_bucket(self.bucket_name)
        self.log.info("Created bucket: %s", self.bucket_name)
        self.s3_url = f"s3://{self.bucket_name}"
        while True:
            self.log.info("iteration %s is started...", self.iteration)
            try:
                if isinstance(self.object_size, dict):
                    file_size = random.randrange(self.object_size["start"], self.object_size["end"])
                else:
                    file_size = self.object_size
                self.log.info("Single object size: %s bytes", file_size)
                write_samples, read_samples, delete_samples = self.get_sample_details(file_size)
                # Write data to fill specified write percentage.
                written_data = 0
                while self.storage_size_to_fill >= written_data:
                    self.write_data(file_size, write_samples)
                    written_data += write_samples * file_size
                self.display_storage_consumed(operation="write")
                # Read data as per specified read percentage.
                read_data = 0
                while self.storage_size_to_read >= read_data:
                    self.read_data(file_size, read_samples, validate=True)
                    read_data += read_samples * file_size
                # Delete data as per specified delete percentage.
                deleted_data = 0
                while self.storage_size_to_delete >= deleted_data:
                    self.delete_data(file_size, delete_samples)
                    deleted_data += file_size * delete_samples
                self.display_storage_consumed(operation="delete")
                if self.total_written_data >= self.size_to_cleanup_all_data:
                    self.log.info("Deleting all object from %s as storage consumption reached "
                                  "limit %s%% ", self.s3_url, self.cleanup_percentage)
                    self.delete_s3_objects(self.bucket_name, object_prefix=self.object_prefix)
                    self.total_written_data *= 0
                    self.log.info("Data cleanup competed...")
                self.display_storage_consumed(operation="")
            except Exception as err:
                self.log.exception("bucket url: {%s}\nException: {%s}", self.s3_url, err)
                assert False, f"bucket url: {self.s3_url}\nException: {err}"
            if (self.finish_time - datetime.now()).total_seconds() < MIN_DURATION:
                self.delete_s3_bucket(self.bucket_name, force=True)
                return True, "Bucket operation execution completed successfully."
            self.log.info("iteration %s is completed...", self.iteration)
            self.iteration += 1

    def display_storage_consumed(self, operation="write"):
        """Display storage consumed after specified operation."""
        if operation:
            self.log.info("Storage consumed %s%% after %s operations.",
                          int(self.total_written_data / self.total_storage * 100), operation)
        else:
            self.log.info("Storage consumed %s%% after iteration %s.",
                          int(self.total_written_data / self.total_storage * 100), self.iteration)

    def get_sample_details(self, file_size: int) -> tuple:
        """
        Get samples for write, read, delete operation to be used in IO.

        :param file_size: Single object size used to calculate the number of sample.
        """
        err_str = "Number of samples '{}' should be greater/equal to number of sessions '{}'."
        # Logic behind adding extra 1 sample is to cover fractional part.
        w_chunks = modf(self.storage_size_to_fill / file_size)
        write_samples = int(w_chunks[1]) + 1 if w_chunks[0] else int(w_chunks[1])
        self.log.info("Number of samples '%s' will be used for write operation.", write_samples)
        assert write_samples >= self.sessions, err_str.format(write_samples, self.sessions)
        r_chunks = modf(self.storage_size_to_read / file_size)
        read_samples = int(r_chunks[1]) + 1 if r_chunks[0] else int(r_chunks[1])
        self.log.info("Number of samples '%s' will be used for read operation.", read_samples)
        assert read_samples >= self.sessions, err_str.format(read_samples, self.sessions)
        d_chunks = modf(self.storage_size_to_delete / file_size)
        delete_samples = int(d_chunks[1]) + 1 if d_chunks[0] else int(d_chunks[1])
        self.log.info("Number of samples '%s' will be used for delete operation.", delete_samples)
        assert delete_samples >= self.sessions, err_str.format(delete_samples, self.sessions)
        return write_samples, read_samples, delete_samples

    def s3bench_cmd(self, object_size: int, number_sample: int) -> str:
        """
        Create s3bench command as per number of sample and object size.

        :param object_size: Object size per sample.
        :param number_sample: Total number samples.
        """
        return f"s3bench -accessKey={self.access_key} -accessSecret={self.secret_key} " \
               f"-endpoint={self.endpoint_url} -bucket={self.bucket_name} " \
               f"-numClients={self.sessions} -skipSSLCertVerification=True " \
               f"-objectNamePrefix={self.object_prefix.format(self.iteration)} " \
               f"-numSamples={number_sample} -objectSize={object_size}b -region {self.region} "

    def cmd_reporting_params(self):
        """Append reporting parameters to s3bench command."""
        return f" -o {self.report_path} -label {self.label} >> {self.log_path} 2>&1"

    def execute_validate_run(self, cmd: str) -> None:
        """
        Execute command and validate the execution report and logs.

        :param cmd: s3bench command to be executed with some workload.
        """
        cmd += self.cmd_reporting_params()
        status, resp = run_local_cmd(cmd)
        assert status, f"Failed execute '{cmd}', response: {resp}"
        status, resp = S3bench.check_log_file_error(self.report_path, self.log_path)
        assert status, f"Observed failures for '{cmd}', response: {resp}"

    def write_data(self, object_size: int, number_sample: int, validate: bool = False) -> None:
        """
        Write data to s3.

        :param object_size: Object size per sample.
        :param number_sample: total number objects to write.
        :param validate: validate data after write.
        """
        self.log.info("Writing data...")
        self.log.info("Single object size: %s, Number of samples: %s", object_size, number_sample)
        cmd = self.s3bench_cmd(object_size, number_sample)
        if validate:
            cmd += " -skipRead -skipCleanup -validate"
        else:
            cmd += " -skipRead -skipCleanup"
        self.execute_validate_run(cmd)
        self.total_written_data += object_size * number_sample
        self.log.info("writing completed...")

    def read_data(self, object_size: int, number_sample: int, validate: bool = False) -> None:
        """
        Read data from s3.

        :param object_size: Object size per sample.
        :param number_sample: total number objects to read.
        :param validate: validate data after reading.
        """
        self.log.info("Reading data...")
        self.log.info("Single object size: %s, Number of samples: %s", object_size, number_sample)
        cmd = self.s3bench_cmd(object_size, number_sample)
        if validate:
            cmd += " -skipWrite -skipCleanup -validate"
        else:
            cmd += " -skipWrite -skipCleanup"
        self.execute_validate_run(cmd)
        self.log.info("Reading completed...")

    def delete_data(self, object_size: int, number_sample: int, validate: bool = False) -> None:
        """
        Delete data from s3.

        :param object_size: Object size per sample.
        :param number_sample: total number objects to delete.
        :param validate: validate data before delete.
        """
        self.log.info("Deleting data...")
        self.log.info("Single object size: %s, Number of samples: %s", object_size, number_sample)
        cmd = self.s3bench_cmd(object_size, number_sample)
        self.total_written_data -= object_size * number_sample
        if validate:
            cmd += " -skipWrite -skipRead -validate"
        else:
            cmd += " -skipWrite -skipRead"
        self.execute_validate_run(cmd)
        self.log.info("Deletion completed...")

    def validate_data(self, object_size: int, number_sample: int) -> None:
        """
        Validate data from s3.

        :param object_size: Object size per sample.
        :param number_sample: total number objects to validate.
        """
        self.log.info("Validating data...")
        self.log.info("Single object size: %s, Number of samples: %s", object_size, number_sample)
        cmd = self.s3bench_cmd(object_size, number_sample)
        cmd += " -skipWrite -skipRead -skipCleanup -validate"
        self.execute_validate_run(cmd)
        self.log.info("Validation completed...")

    def cleanup_data(self, object_size: int, number_sample: int) -> None:
        """
        cleanup data from s3.

        :param object_size: Object size per sample.
        :param number_sample: total number objects to cleanup.
        """
        self.log.info("Cleaning data...")
        self.log.info("Single object size: %s, Number of samples: %s", object_size, number_sample)
        cmd = self.s3bench_cmd(object_size, number_sample)
        cmd += " -skipWrite -skipRead"
        self.execute_validate_run(cmd)
        self.log.info("Data cleanup completed...")

    def object_crud_operations(self, object_size: int, number_sample: int) -> None:
        """
        Perform object crud operations.

        :param object_size: Object size per sample.
        :param number_sample: total number objects to write, read, validate, delete.
        """
        self.log.info("Object CRUD operation started...")
        self.log.info("Single object size: %s, Number of samples: %s", object_size, number_sample)
        cmd = self.s3bench_cmd(object_size, number_sample)
        cmd += " -validate"
        self.execute_validate_run(cmd)
        self.log.info("Object CRUD operation completed...")
