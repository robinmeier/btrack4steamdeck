// Minimal stub of libsamplerate's API for the BTrack reference build.
//
// BTrack only calls src_simple() in resampleOnsetDetectionFunction(), to
// resample the onset detection function buffer to 512 samples. At the
// canonical hop size of 512 the buffer is already 512 samples long
// (ratio == 1.0), and the Python port copies the buffer unchanged in that
// case. This stub does the same, so the C++ reference and the Python port
// can be compared bit-for-bit. It is NOT a general resampler.

#ifndef SAMPLERATE_STUB_H
#define SAMPLERATE_STUB_H

#include <cassert>
#include <cstring>

typedef struct {
    const float* data_in;
    float* data_out;
    long input_frames;
    long output_frames;
    long input_frames_used;
    long output_frames_gen;
    int end_of_input;
    double src_ratio;
} SRC_DATA;

#define SRC_SINC_BEST_QUALITY 0

inline int src_simple(SRC_DATA* data, int /*converter_type*/, int /*channels*/)
{
    assert(data->input_frames == data->output_frames && data->src_ratio == 1.0);
    std::memcpy(data->data_out, data->data_in,
                sizeof(float) * (size_t)data->input_frames);
    data->input_frames_used = data->input_frames;
    data->output_frames_gen = data->output_frames;
    return 0;
}

#endif
