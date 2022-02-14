import pickle
from pathlib import Path
from typing import Dict, Optional

from tqdm.auto import tqdm

from lhotse import CutSet
from lhotse.utils import Pathlike, is_module_available


SHARD_PATTERN = "shard-%06d.tar"


def export_to_webdataset(
    cuts: CutSet,
    output_path: Pathlike,
    shard_size: Optional[int] = None,
    verbose: bool = True,
    audio_format: str = "flac",
    drop_audio: bool = False,
    drop_features: bool = False,
) -> None:
    """
    Saves the CutSet metadata along with audio/features data into a WebDataset archive.
    The audio and feature data is read, decoded, and encoded into ``audio_format`` for audio,
    lilcom for features and arrays with floating point type, and pickle for all other dtypes.
    The intended use of this function is to speed up the I/O in training data pipelines by
    converting random access reads to sequential access reads.

    Supported values for ``audio_format`` are the same as for the ``format`` argument in
    ``torchaudio.save`` function with ``sox_io`` backend.

    If ``shard_size`` is specified, we will leverage WebDataset's ``ShardWriter`` to
    create multiple tarballs with ``shard_size`` items per shard.
    """
    if not is_module_available("webdataset"):
        raise ImportError("Please 'pip install webdataset' first.")
    import webdataset as wds

    output_path = Path(output_path)
    if shard_size is None:
        output_path = output_path.with_suffix(".tar")
        sink = wds.TarWriter(str(output_path))
    else:
        sink = wds.ShardWriter(str(output_path / SHARD_PATTERN), maxcount=shard_size)

    with sink:
        for idx, cut in tqdm(
            enumerate(cuts), desc="Creating WebDataset tarball(s)", disable=not verbose
        ):
            if drop_audio:
                cut = cut.drop_recording()
            if drop_features:
                cut = cut.drop_features()
            cut = cut.move_to_memory(audio_format=audio_format)
            data = pickle.dumps(cut.to_dict())
            sink.write({"__key__": cut.id, "data": data})


class LazyWebdatasetIterator:
    """
    LazyWebdatasetIterator provides the ability to read Lhotse objects from a
    WebDataset tarball on-the-fly, without reading its full contents into memory.

    This class is designed to be a partial "drop-in" replacement for ordinary dicts
    to support lazy loading of RecordingSet, SupervisionSet and CutSet.
    Since it does not support random access reads, some methods of these classes
    might not work properly.

    The behaviour of the underlying ``WebDataset`` instance can be customized by
    providing its kwargs directly to the constructor of this class.
    """

    def __init__(self, path: Pathlike, **wds_kwargs) -> None:
        self.path = path
        self.wds_kwargs = wds_kwargs

    def _reset(self) -> None:
        if not is_module_available("webdataset"):
            raise ImportError("Please 'pip install webdataset' first.")
        import webdataset as wds

        path = Path(self.path)
        if path.is_dir():
            path = sorted(map(str, path.glob("shard-*.tar")))

        self._ds = wds.WebDataset(path, **self.wds_kwargs)
        self._ds_iter = iter(self._ds)

    def __getstate__(self):
        """
        Store the state for pickling -- we'll only store the path + kwargs, and re-initialize
        this iterator when unpickled. This is necessary to transfer this object across processes
        for PyTorch's DataLoader workers.
        """
        state = {"path": self.path, "wds_kwargs": self.wds_kwargs}
        return state

    def __setstate__(self, state: Dict):
        """Restore the state when unpickled."""
        self.__dict__.update(state)

    def __iter__(self):
        self._reset()
        return self

    def __next__(self):
        from lhotse.serialization import deserialize_item

        data_dict = next(self._ds_iter)
        data = pickle.loads(data_dict["data"])
        item = deserialize_item(data)
        return item

    def values(self):
        yield from self

    def keys(self):
        return (item.id for item in self)

    def items(self):
        return ((item.id, item) for item in self)

    def __add__(self, other) -> "LazyIteratorChain":
        from lhotse.serialization import LazyIteratorChain

        return LazyIteratorChain(self, other)