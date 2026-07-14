CC ?= cc
MSYS_SDK_DIR ?= ../../msys-sdk
OUTPUT ?= ../files/bin/msys-hal-native

CPPFLAGS ?=
CFLAGS ?= -O2 -g
WARNINGS := -Wall -Wextra -Wpedantic -Werror
STANDARD := -std=c11

SOURCES := src/native_hal.c $(MSYS_SDK_DIR)/src/mipc.c
HEADERS := $(MSYS_SDK_DIR)/include/msys/mipc.h

.PHONY: all check clean

all: $(OUTPUT)

$(OUTPUT): $(SOURCES) $(HEADERS)
	mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) -I$(MSYS_SDK_DIR)/include $(CFLAGS) $(STANDARD) $(WARNINGS) $(SOURCES) -o $@

check: $(OUTPUT)
	$(OUTPUT) --self-check

clean:
	rm -f $(OUTPUT)
