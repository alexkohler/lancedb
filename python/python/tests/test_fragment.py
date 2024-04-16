# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright The Lance Authors

import json
import multiprocessing
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest
from helper import ProgressForTest
from lance import (
    FragmentMetadata,
    LanceDataset,
    LanceFragment,
    LanceOperation,
    write_dataset,
)
from lance.fragment import write_fragments
from lance.progress import FileSystemFragmentWriteProgress


def test_write_fragment(tmp_path: Path):
    with pytest.raises(OSError):
        LanceFragment.create(tmp_path, pd.DataFrame([]))
    with pytest.raises(OSError):
        LanceFragment.create(tmp_path, pd.DataFrame([{}]))

    df = pd.DataFrame({"a": [1, 2, 3, 4, 5]})
    frag = LanceFragment.create(tmp_path, df)
    meta = frag.to_json()

    assert "id" in meta
    assert "files" in meta
    assert meta["files"][0]["fields"] == [0]


def test_write_fragment_two_phases(tmp_path: Path):
    num_files = 10
    json_array = []
    for i in range(num_files):
        df = pd.DataFrame({"a": [i * 10]})
        frag = LanceFragment.create(tmp_path, df)
        json_array.append(json.dumps(frag.to_json()))

    fragments = [FragmentMetadata.from_json(j) for j in json_array]

    schema = pa.schema([pa.field("a", pa.int64())])

    operation = LanceOperation.Overwrite(schema, fragments)
    dataset = LanceDataset.commit(tmp_path, operation)

    df = dataset.to_table().to_pandas()
    pd.testing.assert_frame_equal(
        df, pd.DataFrame({"a": [i * 10 for i in range(num_files)]})
    )


def test_scan_fragment(tmp_path: Path):
    tab = pa.table({"a": range(100), "b": range(100, 200)})
    ds = write_dataset(tab, tmp_path)
    frag = ds.get_fragments()[0]

    actual = frag.to_table(
        columns=["b"],
        filter="a >= 2",
        offset=20,
    )
    expected = pa.table({"b": range(122, 200)})
    assert actual == expected


def test_scan_fragment_with_dynamic_projection(tmp_path: Path):
    tab = pa.table({"a": range(100), "b": range(100, 200)})
    ds = write_dataset(tab, tmp_path)
    frag = ds.get_fragments()[0]

    actual = frag.to_table(
        columns={"b_proj": "b"},
        filter="a >= 2",
        offset=20,
    )
    expected = pa.table({"b_proj": range(122, 200)})
    assert actual == expected


def test_write_fragments(tmp_path: Path):
    # This will be split across two files if we set the max_bytes_per_file to 1024
    tab = pa.table({
        "a": pa.array(range(1024)),
    })
    progress = ProgressForTest()
    fragments = write_fragments(
        tab,
        tmp_path,
        max_rows_per_group=512,
        max_bytes_per_file=1024,
        progress=progress,
    )
    assert len(fragments) == 2
    assert all(isinstance(f, FragmentMetadata) for f in fragments)
    # progress hook was called for each fragment
    assert progress.begin_called == 2
    assert progress.complete_called == 2


def test_write_fragment_with_progress(tmp_path: Path):
    df = pd.DataFrame({"a": [10 * 10]})
    progress = ProgressForTest()
    LanceFragment.create(tmp_path, df, progress=progress)
    assert progress.begin_called == 1
    assert progress.complete_called == 1


def failing_write(progress_uri: str, dataset_uri: str):
    # re-create progress so we don't have to pickle it
    progress = FileSystemFragmentWriteProgress(
        progress_uri, metadata={"test_key": "test_value"}
    )
    arr = pa.array(range(100))
    batch = pa.record_batch([arr], names=["a"])

    def data():
        yield batch
        raise Exception("Something went wrong!")

    reader = pa.RecordBatchReader.from_batches(batch.schema, data())
    with pytest.raises(Exception):
        LanceFragment.create(
            dataset_uri,
            reader,
            fragment_id=1,
            progress=progress,
        )


def test_dataset_progress(tmp_path: Path):
    dataset_uri = tmp_path / "dataset"
    progress_uri = tmp_path / "progress"
    data = pa.table({"a": range(100)})
    progress = FileSystemFragmentWriteProgress(progress_uri)
    fragment = LanceFragment.create(
        dataset_uri,
        data,
        progress=progress,
    )

    # In-progress file should be deleted
    assert not (progress_uri / "fragment_0.in_progress").exists()

    # Metadata should be written
    with open(progress_uri / "fragment_0.json") as f:
        metadata = json.load(f)

    assert metadata["id"] == 0
    assert len(metadata["files"]) == 1
    # Fragments aren't exactly equal, because the file was written before
    # physical_rows was known.
    assert (
        fragment.data_files()
        == FragmentMetadata.from_json(json.dumps(metadata)).data_files()
    )

    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=failing_write, args=(progress_uri, dataset_uri))
    p.start()
    try:
        p.join()
    except Exception:
        # Allow a crash to happen
        pass

    # In-progress file should be present
    with open(progress_uri / "fragment_1.in_progress") as f:
        progress_data = json.load(f)
    assert progress_data["fragment_id"] == 1
    assert isinstance(progress_data["multipart_id"], str)
    # progress contains custom metadata
    assert progress_data["metadata"]["test_key"] == "test_value"

    # Metadata should be written
    with open(progress_uri / "fragment_1.json") as f:
        metadata = json.load(f)
    assert metadata["id"] == 1

    progress.cleanup_partial_writes(str(dataset_uri))

    assert not (progress_uri / "fragment_1.json").exists()
    assert not (progress_uri / "fragment_1.in_progress").exists()


def test_fragment_meta():
    # Intentionally leaving off column_indices / version fields to make sure
    # we can handle backwards compatibility (though not clear we need to)
    data = {
        "id": 0,
        "files": [
            {"path": "0.lance", "fields": [0]},
            {"path": "1.lance", "fields": [1]},
        ],
        "deletion_file": None,
        "physical_rows": 100,
    }
    meta = FragmentMetadata.from_json(json.dumps(data))

    assert meta.id == 0
    assert len(meta.data_files()) == 2
    assert meta.data_files()[0].path() == "0.lance"
    assert meta.data_files()[1].path() == "1.lance"

    assert repr(meta) == (
        'Fragment { id: 0, files: [DataFile { path: "0.lance", fields: [0], '
        "column_indices: [], file_major_version: 0, file_minor_version: 0 }, "
        'DataFile { path: "1.lance", fields: [1], column_indices: [], '
        "file_major_version: 0, file_minor_version: 0 }], deletion_file: None, "
        "physical_rows: Some(100) }"
    )
