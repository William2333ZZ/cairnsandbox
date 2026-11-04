#include "stats.h"

void stats_compute(const long *xs, int n, long *sum, long *max) {
    *sum = 0;
    *max = 0;
    for (int i = 1; i < n; i++) {
        *sum += xs[i];
        if (xs[i] > *max) *max = xs[i];
    }
}
