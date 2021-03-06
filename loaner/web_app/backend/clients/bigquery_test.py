# Copyright 2018 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for backend.clients.bigquery."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import datetime

from absl.testing import parameterized

import mock

# pylint: disable=g-bad-import-order
from loaner.web_app.backend.common import google_cloud_lib_fixer  # pylint: disable=unused-import
from google import cloud
from google.cloud import bigquery as gcloud_bq

from google.appengine.ext import ndb
# pylint: enable=g-bad-import-order

from loaner.web_app.backend.clients import bigquery
from loaner.web_app.backend.models import bigquery_row_model
from loaner.web_app.backend.models import device_model
from loaner.web_app.backend.testing import loanertest


class BigQueryClientTest(loanertest.TestCase, parameterized.TestCase):
  """Tests for BigQueryClient."""

  def setUp(self):
    super(BigQueryClientTest, self).setUp()
    bq_patcher = mock.patch.object(gcloud_bq, 'Client', autospec=True)
    self.addCleanup(bq_patcher.stop)
    self.bq_mock = bq_patcher.start()
    self.dataset_ref = mock.Mock(spec=gcloud_bq.DatasetReference)
    self.table = mock.Mock(spec=gcloud_bq.Table)
    self.table.schema = []
    self.dataset_ref.table.return_value = self.table
    with mock.patch.object(
        bigquery.BigQueryClient, '__init__', return_value=None):
      self.client = bigquery.BigQueryClient()
      self.client._client = self.bq_mock()
      self.client._dataset_ref = self.dataset_ref
      self.client._client.insert_rows.return_value = None
      self.client._client.get_table.return_value = self.table
    self.nested_schema = [
        gcloud_bq.SchemaField('nested_string_attribute', 'STRING', 'NULLABLE')]
    self.entity_schema = [
        gcloud_bq.SchemaField('string_attribute', 'STRING', 'NULLABLE'),
        gcloud_bq.SchemaField('integer_attribute', 'INTEGER', 'NULLABLE'),
        gcloud_bq.SchemaField('boolean_attribute', 'BOOLEAN', 'NULLABLE'),
        gcloud_bq.SchemaField(
            'nested_attribute', 'RECORD', 'NULLABLE', fields=self.nested_schema)
    ]

    test_device = device_model.Device(
        serial_number='abc123', chrome_device_id='123123')
    test_device.put()
    test_row = bigquery_row_model.BigQueryRow.add(
        test_device, datetime.datetime.utcnow(),
        loanertest.USER_EMAIL, 'Enroll', 'This is a test')
    self.test_row_dict = test_row.to_json_dict()
    self.test_table = [(self.test_row_dict['ndb_key'],
                        self.test_row_dict['timestamp'],
                        self.test_row_dict['actor'],
                        self.test_row_dict['method'],
                        self.test_row_dict['summary'],
                        self.test_row_dict['entity'])]

  @mock.patch.object(bigquery, '_generate_schema')
  def test_initialize_tables(self, mock_schema):
    self.client.initialize_tables()

    mock_schema.assert_called()
    # Using assert foo.called here because assert_called() breaks here
    # in OSS and I'll be honest I'm sick of trying to debug it.
    assert self.client._client.create_dataset.called
    assert self.client._client.create_table.called

  @mock.patch.object(
      bigquery, '_generate_schema', return_value=mock.Mock())
  @mock.patch.object(bigquery.BigQueryClient, '_create_table')
  def test_initialize_tables__dataset_exists(self, mock_table, mock_schema):
    self.client._client.create_dataset.side_effect = cloud.exceptions.Conflict(
        'Already Exists: Dataset Loaner')

    with mock.patch.object(gcloud_bq, 'Dataset') as mock_dataset:
      mock_dataset.dataset_id = 'test'
      self.client.initialize_tables()

    mock_table.assert_called()
    assert self.client._client.create_dataset.called

  def test_stream_table(self):
    self.client.stream_table('Device', self.test_table)
    self.client._client.insert_rows.assert_called_once_with(
        self.table, self.test_table)

  def test_stream_row_no_table(self):
    self.client._client.get_table.side_effect = cloud.exceptions.NotFound(
        'Table does not exist')
    self.assertRaises(
        bigquery.GetTableError,
        self.client.stream_table, 'Device', self.test_table)

  def test_stream_row_bq_errors(self):
    self.client._client.insert_rows.return_value = 'Oh no it exploded'
    self.assertRaises(
        bigquery.InsertError,
        self.client.stream_table, 'Device', self.test_table)

  def test_get_device_info(self):
    test_serial = 'ABC1234'
    expected_results = [('ABC1234', 'test@', '0000')]
    self.client._client.query.return_value = expected_results

    results = self.client.get_device_info(test_serial)

    self.assertEqual(results, expected_results)

  def test_generate_schema_no_entity(self):
    generated_schema = bigquery._generate_schema()

    self.assertLen(generated_schema, 5)
    self.assertIsInstance(generated_schema[0], gcloud_bq.SchemaField)

  def test_generate_schema_entity(self):
    entity_fields = [gcloud_bq.SchemaField('test', 'STRING', 'REQUIRED')]

    generated_schema = bigquery._generate_schema(entity_fields)
    self.assertLen(generated_schema, 6)
    self.assertEqual(generated_schema[5].fields[0].name, 'test')

  def test_generate_entity_schema(self):

    class NestedTestModel(ndb.Model):
      nested_string_attribute = ndb.StringProperty()

    class TestModel(ndb.Model):
      string_attribute = ndb.StringProperty()
      integer_attribute = ndb.IntegerProperty()
      boolean_attribute = ndb.BooleanProperty()
      nested_attribute = ndb.StructuredProperty(NestedTestModel)

    schema = bigquery._generate_entity_schema(TestModel())
    expected_schema_names = _populate_schema_names(self.entity_schema)
    schema_names = _populate_schema_names(schema)
    self.assertCountEqual(expected_schema_names, schema_names)

  def test_merge_schemas(self):
    schema_1 = [
        gcloud_bq.SchemaField('ndb_key', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('timestamp', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('actor', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('method', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('summary', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField(
            'entity', 'RECORD', 'NULLABLE', fields=tuple(self.entity_schema))
    ]
    nested_schema_2 = [
        gcloud_bq.SchemaField(
            'new_nested_string_attribute', 'STRING', 'NULLABLE')]
    entity_schema_2 = [
        gcloud_bq.SchemaField('new_string_attribute', 'STRING', 'NULLABLE'),
        gcloud_bq.SchemaField(
            'nested_attribute', 'RECORD', 'NULLABLE', fields=nested_schema_2)
    ]
    schema_2 = [
        gcloud_bq.SchemaField('ndb_key', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('timestamp', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('actor', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('method', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('summary', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField(
            'entity', 'RECORD', 'NULLABLE', fields=tuple(entity_schema_2))
    ]
    # The expected schema should be a union of the two schemas.
    expected_entity_schema = [
        gcloud_bq.SchemaField('string_attribute', 'STRING', 'NULLABLE'),
        gcloud_bq.SchemaField('integer_attribute', 'INTEGER', 'NULLABLE'),
        gcloud_bq.SchemaField('boolean_attribute', 'BOOLEAN', 'NULLABLE'),
        gcloud_bq.SchemaField('new_string_attribute', 'STRING', 'NULLABLE'),
        gcloud_bq.SchemaField(
            'nested_attribute', 'RECORD', 'NULLABLE',
            fields=self.nested_schema + nested_schema_2)
    ]
    expected_schema = [
        gcloud_bq.SchemaField('ndb_key', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('timestamp', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('actor', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('method', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField('summary', 'STRING', 'REQUIRED'),
        gcloud_bq.SchemaField(
            'entity', 'RECORD', 'NULLABLE', fields=expected_entity_schema)
    ]
    merged_schema = bigquery._merge_schemas(schema_1, schema_2)
    self.assertEqual(merged_schema, expected_schema)

  @parameterized.named_parameters(
      ('type',
       [gcloud_bq.SchemaField('new_string_attribute', 'STRING', 'NULLABLE')],
       [gcloud_bq.SchemaField('new_string_attribute', 'INTEGER', 'NULLABLE')],
       bigquery.SchemaFieldTypeError),
      ('mode',
       [gcloud_bq.SchemaField('new_string_attribute', 'STRING', 'NULLABLE')],
       [gcloud_bq.SchemaField('new_string_attribute', 'STRING', 'REQUIRED')],
       bigquery.SchemaFieldModeError))
  def test_merge_schemas_field_error(self, schema_1, schema_2, error):
    with self.assertRaises(error):
      bigquery._merge_schemas(schema_1, schema_2)


def _populate_schema_names(schema):
  """Creates a list with the names that are inside of the schema.

  Args:
    schema: List[bigquery.SchemaField], a list of bigquery.SchemaField objects.

  Returns:
    A list containing the names of the schema.
  """
  names = []
  for name in schema:
    names.append(name.name)
  return names


if __name__ == '__main__':
  loanertest.main()
