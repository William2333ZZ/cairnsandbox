#include <stdio.h>
#include <stdlib.h>
#include "stats.h"

int main(void) {
    long xs[1024]; int n = 0; long v;
    while (n < 1024 && scanf("%ld", &v) == 1) xs[n++] = v;
    long sum, max;
    stats_compute(xs, n, &sum, &max);
    printf("count=%d sum=%ld max=%ld\n", n, sum, max);
    return 0;
}
