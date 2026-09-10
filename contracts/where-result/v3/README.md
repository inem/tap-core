# tap.where-result/v3

Extends v2 with one Core-owned capture-storage observation. The stream address
is derived from the declared profile data address. One `lstat` supplies presence,
kind and regular-file byte size; the command never reads the stream to count
records.

`rotation_policy` reports the effective runtime limits and whether they came
from defaults or explicit profile configuration. These inventory facts do not
claim that the writer is healthy or that traffic is flowing.
