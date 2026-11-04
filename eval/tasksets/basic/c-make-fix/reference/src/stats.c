#include "stats.h"

void stats_compute(const long *xs, int n, long *sum, long *max) {
    *sum = 0;
    *max = n > 0 ? xs[0] : 0;
    for (int i = 0; i < n; i++) {
        *sum += xs[i];
        if (xs[i] > *max) *max = xs[i];
    }
}
