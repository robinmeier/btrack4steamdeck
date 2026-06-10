// Reference harness: feed raw float64 mono audio (44.1 kHz) from a file
// through the original BTrack C++ implementation and print, for every
// 512-sample hop:
//
//   <hop index> <odf sample> <beat due (0/1)> <estimated tempo>
//
// Used by tests/compare_with_cpp.py to validate the Python port.

#include <cstdio>
#include <cstdlib>
#include <vector>
#include "BTrack.h"
#include "OnsetDetectionFunction.h"

int main(int argc, char** argv)
{
    if (argc != 2)
    {
        std::fprintf(stderr, "usage: %s <raw float64 audio file>\n", argv[0]);
        return 1;
    }

    FILE* f = std::fopen(argv[1], "rb");
    if (!f)
    {
        std::perror("fopen");
        return 1;
    }

    const int hop = 512;
    OnsetDetectionFunction odf(hop, 2 * hop, ComplexSpectralDifferenceHWR, HanningWindow);
    BTrack bt(hop);

    std::vector<double> buffer(hop);
    long hopIndex = 0;

    while (std::fread(buffer.data(), sizeof(double), hop, f) == (size_t)hop)
    {
        double odfSample = odf.calculateOnsetDetectionFunctionSample(buffer.data());
        bt.processOnsetDetectionFunctionSample(odfSample);
        std::printf("%ld %.17g %d %.17g\n",
                    hopIndex,
                    odfSample,
                    bt.beatDueInCurrentFrame() ? 1 : 0,
                    bt.getCurrentTempoEstimate());
        hopIndex++;
    }

    std::fclose(f);
    return 0;
}
