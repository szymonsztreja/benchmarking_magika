import io
from magika import Magika, PredictionMode


class SpyBytesIO(io.BytesIO):
    def seek(self, offset, whence=io.SEEK_SET):
        pos = super().seek(offset, whence)
        print(f"seek({offset}, {whence}) -> {pos}")
        return pos

    def read(self, size=-1):
        data = super().read(size)
        print(f"read({size}) -> {len(data)} bytes")
        return data

    def tell(self):
        pos = super().tell()
        print(f"tell() -> {pos}")
        return pos


data_1 = b"import os\n" + b"print('hello')\n" * 100_000  # ~1.5 MB
data_2 = b"x" * 5000
data_3 = b" " * 4000 + b"print('hi')\n"
data_4 = b" " * 4000 + b"hi\n"


m = Magika(prediction_mode=PredictionMode.BEST_GUESS)

# bytes_list = [("abc", b"abc"), ("data_1", data_1), ("data_2", data_2),
#                    ("data_3", data_3), ("data_4", data_4)]

bytes_list = [("print_hi", data_3)]

for name, data in bytes_list:
    print(f"--- {name} (size={len(data)})")
    res = m.identify_stream(SpyBytesIO(data))
    print(f"\n\n\n dl={res.dl.label}\n output={res.output.label}\n score={res.score:.3f}")
    print(f"Prediction overwirte reason = {res.prediction.overwrite_reason}\n\n\n")