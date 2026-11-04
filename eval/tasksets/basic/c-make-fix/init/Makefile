CC ?= gcc
CFLAGS ?= -O1 -Wall -Isrc

stats: src/main.o
	$(CC) $(CFLAGS) -o $@ $^

src/%.o: src/%.c src/stats.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f stats src/*.o

.PHONY: clean
