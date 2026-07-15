#define _POSIX_C_SOURCE 200809L

#include "msys/mipc.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#ifndef O_CLOEXEC
#define O_CLOEXEC 0
#endif
#ifndef O_NOFOLLOW
#define O_NOFOLLOW 0
#endif

#define HAL_VERSION "0.2.11"
#define MANAGER_SCHEMA "org.msys.hal.manager.v1"
#define NATIVE_SCHEMA "org.msys.hal.native-manager.v1"
#define COMPONENT_ID "org.msys.hal.linux:native-manager"
#define MAX_REQUEST_JSON (64u * 1024u)
#define MAX_RESPONSE_JSON (192u * 1024u)
#define MAX_TOKENS 2048
#define MAX_DEVICES 256
#define MAX_ENTRIES 128
#define MAX_NAME 128
#define DOMAIN_COUNT 8
#define WPA_RESPONSE_CAPACITY (64u * 1024u)
#define WPA_COMMAND_CAPACITY 4096u
#define MAX_WIFI_SCAN_RESULTS 24u
#define MAX_WIFI_NETWORKS 24u

#define MUTABLE_NONE 0
#define MUTABLE_STATE 1
#define MUTABLE_ACTION 2

/* Linux Bluetooth Management ABI.  These constants are part of the stable
 * kernel/BlueZ control protocol and avoid a libbluetooth or D-Bus dependency. */
#define MSYS_AF_BLUETOOTH 31
#define MSYS_BTPROTO_HCI 1
#define MSYS_HCI_DEV_NONE 0xffffu
#define MSYS_HCI_CHANNEL_CONTROL 3u
#define MGMT_EV_CMD_COMPLETE 0x0001u
#define MGMT_EV_CMD_STATUS 0x0002u
#define MGMT_EV_DEVICE_FOUND 0x0012u
#define MGMT_OP_READ_INDEX_LIST 0x0003u
#define MGMT_OP_READ_INFO 0x0004u
#define MGMT_OP_SET_POWERED 0x0005u
#define MGMT_OP_START_DISCOVERY 0x0023u
#define MGMT_OP_STOP_DISCOVERY 0x0024u
#define MGMT_SETTING_POWERED 0x00000001u
#define MGMT_SETTING_DISCOVERABLE 0x00000008u
#define MGMT_DISCOVERY_ALL 0x07u
#define MGMT_PACKET_CAPACITY 4096u
#define MAX_BLUETOOTH_DISCOVERED 24u

typedef struct {
    uint16_t family;
    uint16_t device;
    uint16_t channel;
} MsysSockaddrHci;

typedef struct {
    int index;
    int powered;
    int discoverable;
    char address[18];
    char name[64];
} BluetoothInfo;

typedef struct {
    char address[18];
    char name[64];
    int address_type;
    int rssi;
} BluetoothFound;

static BluetoothFound bluetooth_found[MAX_BLUETOOTH_DISCOVERED];
static size_t bluetooth_found_count = 0u;
static char bluetooth_management_error[64] = "not-probed";

static void bluetooth_error(const char *stage, int code)
{
    (void)snprintf(
        bluetooth_management_error,
        sizeof(bluetooth_management_error),
        "%s:%d",
        stage,
        code
    );
}

typedef enum {
    JT_OBJECT,
    JT_ARRAY,
    JT_STRING,
    JT_PRIMITIVE
} JsonType;

typedef struct {
    JsonType type;
    int start;
    int end;
    int parent;
} JsonToken;

typedef struct {
    const char *text;
    size_t length;
    size_t position;
    JsonToken *tokens;
    int capacity;
    int count;
} JsonParser;

typedef struct {
    char *data;
    size_t length;
    size_t capacity;
    int failed;
} JsonBuffer;

typedef enum {
    DEVICE_POWER,
    DEVICE_THERMAL,
    DEVICE_BACKLIGHT,
    DEVICE_INPUT,
    DEVICE_NETWORK,
    DEVICE_BLUETOOTH,
    DEVICE_RFKILL_NETWORK,
    DEVICE_RFKILL_BLUETOOTH
} DeviceKind;

typedef struct {
    DeviceKind kind;
    char domain[24];
    char name[MAX_NAME + 1];
    char label[MAX_NAME + 1];
    char detail[MAX_NAME + 1];
    int64_t maximum;
    int mutable;
} Device;

typedef struct {
    Device items[MAX_DEVICES];
    size_t count;
} DeviceList;

static const char *const DOMAINS[DOMAIN_COUNT] = {
    "power", "thermal", "backlight", "display", "display-output", "input",
    "network", "bluetooth"
};

static uint64_t revision_number = 0;

static size_t utf8_sequence_length(const unsigned char *cursor);
static size_t list_entries(
    const char *root,
    const char *prefix,
    char names[MAX_ENTRIES][MAX_NAME + 1]
);
static int parse_decimal(const char *text, int minimum, int maximum, int *value);

static void skip_space(JsonParser *parser)
{
    while (parser->position < parser->length &&
           isspace((unsigned char)parser->text[parser->position]) != 0) {
        ++parser->position;
    }
}

static int new_token(JsonParser *parser, JsonType type, int start, int parent)
{
    JsonToken *token;
    if (parser->count >= parser->capacity) {
        return -1;
    }
    token = &parser->tokens[parser->count];
    token->type = type;
    token->start = start;
    token->end = -1;
    token->parent = parent;
    return parser->count++;
}

static int hex_digit(char value)
{
    return (value >= '0' && value <= '9') ||
           (value >= 'a' && value <= 'f') ||
           (value >= 'A' && value <= 'F');
}

static int parse_string(JsonParser *parser, int parent)
{
    int token_index;
    if (parser->position >= parser->length || parser->text[parser->position] != '"') {
        return -1;
    }
    ++parser->position;
    token_index = new_token(parser, JT_STRING, (int)parser->position, parent);
    if (token_index < 0) {
        return -1;
    }
    while (parser->position < parser->length) {
        unsigned char value = (unsigned char)parser->text[parser->position++];
        if (value == '"') {
            parser->tokens[token_index].end = (int)parser->position - 1;
            return token_index;
        }
        if (value < 0x20u) {
            return -1;
        }
        if (value == '\\') {
            size_t index;
            char escaped;
            if (parser->position >= parser->length) {
                return -1;
            }
            escaped = parser->text[parser->position++];
            if (strchr("\"\\/bfnrt", escaped) != NULL) {
                continue;
            }
            if (escaped != 'u' || parser->length - parser->position < 4u) {
                return -1;
            }
            for (index = 0; index < 4u; ++index) {
                if (!hex_digit(parser->text[parser->position + index])) {
                    return -1;
                }
            }
            parser->position += 4u;
        }
    }
    return -1;
}

static int parse_value(JsonParser *parser, int parent, unsigned depth);

static int parse_object(JsonParser *parser, int parent, unsigned depth)
{
    int object_index;
    if (depth > 16u || parser->text[parser->position] != '{') {
        return -1;
    }
    object_index = new_token(parser, JT_OBJECT, (int)parser->position, parent);
    if (object_index < 0) {
        return -1;
    }
    ++parser->position;
    skip_space(parser);
    if (parser->position < parser->length && parser->text[parser->position] == '}') {
        parser->tokens[object_index].end = (int)++parser->position;
        return object_index;
    }
    for (;;) {
        if (parse_string(parser, object_index) < 0) {
            return -1;
        }
        skip_space(parser);
        if (parser->position >= parser->length || parser->text[parser->position++] != ':') {
            return -1;
        }
        skip_space(parser);
        if (parse_value(parser, object_index, depth + 1u) < 0) {
            return -1;
        }
        skip_space(parser);
        if (parser->position >= parser->length) {
            return -1;
        }
        if (parser->text[parser->position] == '}') {
            parser->tokens[object_index].end = (int)++parser->position;
            return object_index;
        }
        if (parser->text[parser->position++] != ',') {
            return -1;
        }
        skip_space(parser);
    }
}

static int parse_array(JsonParser *parser, int parent, unsigned depth)
{
    int array_index;
    if (depth > 16u || parser->text[parser->position] != '[') {
        return -1;
    }
    array_index = new_token(parser, JT_ARRAY, (int)parser->position, parent);
    if (array_index < 0) {
        return -1;
    }
    ++parser->position;
    skip_space(parser);
    if (parser->position < parser->length && parser->text[parser->position] == ']') {
        parser->tokens[array_index].end = (int)++parser->position;
        return array_index;
    }
    for (;;) {
        if (parse_value(parser, array_index, depth + 1u) < 0) {
            return -1;
        }
        skip_space(parser);
        if (parser->position >= parser->length) {
            return -1;
        }
        if (parser->text[parser->position] == ']') {
            parser->tokens[array_index].end = (int)++parser->position;
            return array_index;
        }
        if (parser->text[parser->position++] != ',') {
            return -1;
        }
        skip_space(parser);
    }
}

static int valid_number(const char *value, size_t length)
{
    size_t position = 0;
    if (position < length && value[position] == '-') {
        ++position;
    }
    if (position >= length) {
        return 0;
    }
    if (value[position] == '0') {
        ++position;
    } else {
        if (!isdigit((unsigned char)value[position])) {
            return 0;
        }
        while (position < length && isdigit((unsigned char)value[position])) {
            ++position;
        }
    }
    if (position < length && value[position] == '.') {
        ++position;
        if (position >= length || !isdigit((unsigned char)value[position])) {
            return 0;
        }
        while (position < length && isdigit((unsigned char)value[position])) {
            ++position;
        }
    }
    if (position < length && (value[position] == 'e' || value[position] == 'E')) {
        ++position;
        if (position < length && (value[position] == '+' || value[position] == '-')) {
            ++position;
        }
        if (position >= length || !isdigit((unsigned char)value[position])) {
            return 0;
        }
        while (position < length && isdigit((unsigned char)value[position])) {
            ++position;
        }
    }
    return position == length;
}

static int parse_primitive(JsonParser *parser, int parent)
{
    size_t start = parser->position;
    int token_index;
    while (parser->position < parser->length &&
           strchr(" \t\r\n,]}", parser->text[parser->position]) == NULL) {
        ++parser->position;
    }
    if (parser->position == start) {
        return -1;
    }
    if (!((parser->position - start == 4u &&
           memcmp(parser->text + start, "true", 4u) == 0) ||
          (parser->position - start == 5u &&
           memcmp(parser->text + start, "false", 5u) == 0) ||
          (parser->position - start == 4u &&
           memcmp(parser->text + start, "null", 4u) == 0) ||
          valid_number(parser->text + start, parser->position - start))) {
        return -1;
    }
    token_index = new_token(parser, JT_PRIMITIVE, (int)start, parent);
    if (token_index < 0) {
        return -1;
    }
    parser->tokens[token_index].end = (int)parser->position;
    return token_index;
}

static int parse_value(JsonParser *parser, int parent, unsigned depth)
{
    if (parser->position >= parser->length) {
        return -1;
    }
    if (parser->text[parser->position] == '{') {
        return parse_object(parser, parent, depth);
    }
    if (parser->text[parser->position] == '[') {
        return parse_array(parser, parent, depth);
    }
    if (parser->text[parser->position] == '"') {
        return parse_string(parser, parent);
    }
    return parse_primitive(parser, parent);
}

static int parse_json(const char *text, size_t length, JsonToken *tokens, int capacity)
{
    JsonParser parser;
    int root;
    if (text == NULL || length == 0u || length > MAX_REQUEST_JSON) {
        return -1;
    }
    parser.text = text;
    parser.length = length;
    parser.position = 0u;
    parser.tokens = tokens;
    parser.capacity = capacity;
    parser.count = 0;
    skip_space(&parser);
    root = parse_value(&parser, -1, 0u);
    if (root != 0) {
        return -1;
    }
    skip_space(&parser);
    return parser.position == length ? parser.count : -1;
}

static int token_next(const JsonToken *tokens, int count, int index)
{
    int end;
    if (index < 0 || index >= count) {
        return count;
    }
    end = tokens[index].end;
    ++index;
    while (index < count && tokens[index].start < end) {
        ++index;
    }
    return index;
}

static int raw_string_equal(const char *json, const JsonToken *token, const char *expected)
{
    size_t length;
    if (token->type != JT_STRING || expected == NULL) {
        return 0;
    }
    length = (size_t)(token->end - token->start);
    return strlen(expected) == length &&
           memcmp(json + token->start, expected, length) == 0;
}

static int object_field(
    const char *json,
    const JsonToken *tokens,
    int count,
    int object,
    const char *key
)
{
    int index;
    int found = -1;
    if (object < 0 || object >= count || tokens[object].type != JT_OBJECT) {
        return -2;
    }
    index = object + 1;
    while (index < count && tokens[index].start < tokens[object].end) {
        int value = index + 1;
        if (tokens[index].parent != object || tokens[index].type != JT_STRING ||
            value >= count || tokens[value].parent != object) {
            return -2;
        }
        if (raw_string_equal(json, &tokens[index], key)) {
            if (found >= 0) {
                return -2;
            }
            found = value;
        }
        index = token_next(tokens, count, value);
    }
    return found;
}

static int object_validate_fields(
    const char *json,
    const JsonToken *tokens,
    int count,
    int object,
    const char *const *allowed,
    size_t allowed_count
)
{
    int index;
    if (object < 0 || object >= count || tokens[object].type != JT_OBJECT) {
        return 0;
    }
    index = object + 1;
    while (index < count && tokens[index].start < tokens[object].end) {
        int value = index + 1;
        int previous;
        size_t allowed_index;
        int known = 0;
        if (tokens[index].parent != object || tokens[index].type != JT_STRING ||
            value >= count || tokens[value].parent != object) {
            return 0;
        }
        for (allowed_index = 0; allowed_index < allowed_count; ++allowed_index) {
            if (raw_string_equal(json, &tokens[index], allowed[allowed_index])) {
                known = 1;
                break;
            }
        }
        if (!known) {
            return 0;
        }
        previous = object + 1;
        while (previous < index) {
            int previous_value = previous + 1;
            size_t current_length = (size_t)(tokens[index].end - tokens[index].start);
            size_t previous_length = (size_t)(tokens[previous].end - tokens[previous].start);
            if (tokens[previous].parent == object &&
                current_length == previous_length &&
                memcmp(json + tokens[index].start,
                       json + tokens[previous].start,
                       current_length) == 0) {
                return 0;
            }
            previous = token_next(tokens, count, previous_value);
        }
        index = token_next(tokens, count, value);
    }
    return 1;
}

static int copy_string(
    const char *json,
    const JsonToken *token,
    char *output,
    size_t capacity
)
{
    size_t read_position;
    size_t write_position = 0u;
    if (token->type != JT_STRING || capacity == 0u) {
        return 0;
    }
    read_position = (size_t)token->start;
    while (read_position < (size_t)token->end) {
        unsigned char value = (unsigned char)json[read_position++];
        if (value == '\\') {
            char escaped;
            if (read_position >= (size_t)token->end) {
                return 0;
            }
            escaped = json[read_position++];
            switch (escaped) {
            case '"': value = '"'; break;
            case '\\': value = '\\'; break;
            case '/': value = '/'; break;
            case 'b': value = '\b'; break;
            case 'f': value = '\f'; break;
            case 'n': value = '\n'; break;
            case 'r': value = '\r'; break;
            case 't': value = '\t'; break;
            default:
                return 0;
            }
        }
        if (value < 0x20u || value > 0x7eu || write_position + 1u >= capacity) {
            return 0;
        }
        output[write_position++] = (char)value;
    }
    output[write_position] = '\0';
    return 1;
}

static int copy_utf8_string(
    const char *json,
    const JsonToken *token,
    char *output,
    size_t capacity,
    size_t maximum_bytes
)
{
    size_t read_position;
    size_t write_position = 0u;
    size_t position = 0u;
    if (token->type != JT_STRING || capacity == 0u || maximum_bytes >= capacity) {
        return 0;
    }
    read_position = (size_t)token->start;
    while (read_position < (size_t)token->end) {
        unsigned char value = (unsigned char)json[read_position++];
        if (value == '\\') {
            char escaped;
            if (read_position >= (size_t)token->end) {
                return 0;
            }
            escaped = json[read_position++];
            switch (escaped) {
            case '"': value = '"'; break;
            case '\\': value = '\\'; break;
            case '/': value = '/'; break;
            default: return 0;
            }
        }
        if (value < 0x20u || write_position >= maximum_bytes ||
            write_position + 1u >= capacity) {
            return 0;
        }
        output[write_position++] = (char)value;
    }
    output[write_position] = '\0';
    while (position < write_position) {
        const unsigned char *cursor = (const unsigned char *)output + position;
        size_t length = utf8_sequence_length(cursor);
        if (length == 0u || length > write_position - position) {
            return 0;
        }
        position += length;
    }
    return write_position > 0u;
}

static int token_bool(const char *json, const JsonToken *token, int *value)
{
    size_t length;
    if (token->type != JT_PRIMITIVE) {
        return 0;
    }
    length = (size_t)(token->end - token->start);
    if (length == 4u && memcmp(json + token->start, "true", 4u) == 0) {
        *value = 1;
        return 1;
    }
    if (length == 5u && memcmp(json + token->start, "false", 5u) == 0) {
        *value = 0;
        return 1;
    }
    return 0;
}

static int token_i64(const char *json, const JsonToken *token, int64_t *value)
{
    char text[64];
    char *end = NULL;
    long long parsed;
    size_t length;
    if (token->type != JT_PRIMITIVE) {
        return 0;
    }
    length = (size_t)(token->end - token->start);
    if (length == 0u || length >= sizeof(text)) {
        return 0;
    }
    memcpy(text, json + token->start, length);
    text[length] = '\0';
    if (strchr(text, '.') != NULL || strchr(text, 'e') != NULL || strchr(text, 'E') != NULL) {
        return 0;
    }
    errno = 0;
    parsed = strtoll(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        return 0;
    }
    *value = (int64_t)parsed;
    return 1;
}

static void buffer_init(JsonBuffer *buffer)
{
    buffer->data = (char *)malloc(MAX_RESPONSE_JSON);
    buffer->length = 0u;
    buffer->capacity = buffer->data == NULL ? 0u : MAX_RESPONSE_JSON;
    buffer->failed = buffer->data == NULL;
    if (!buffer->failed) {
        buffer->data[0] = '\0';
    }
}

static void buffer_free(JsonBuffer *buffer)
{
    free(buffer->data);
    buffer->data = NULL;
    buffer->length = 0u;
    buffer->capacity = 0u;
    buffer->failed = 1;
}

static void buffer_append_n(JsonBuffer *buffer, const char *text, size_t length)
{
    if (buffer->failed || length > buffer->capacity - buffer->length - 1u) {
        buffer->failed = 1;
        return;
    }
    memcpy(buffer->data + buffer->length, text, length);
    buffer->length += length;
    buffer->data[buffer->length] = '\0';
}

static void buffer_append(JsonBuffer *buffer, const char *text)
{
    buffer_append_n(buffer, text, strlen(text));
}

static void buffer_format(JsonBuffer *buffer, const char *format, ...)
{
    va_list arguments;
    int required;
    if (buffer->failed) {
        return;
    }
    va_start(arguments, format);
    required = vsnprintf(
        buffer->data + buffer->length,
        buffer->capacity - buffer->length,
        format,
        arguments
    );
    va_end(arguments);
    if (required < 0 || (size_t)required >= buffer->capacity - buffer->length) {
        buffer->failed = 1;
        return;
    }
    buffer->length += (size_t)required;
}

static size_t utf8_sequence_length(const unsigned char *cursor)
{
    unsigned char first = cursor[0];
    if (first < 0x80u) {
        return 1u;
    }
    if (first >= 0xc2u && first <= 0xdfu &&
        cursor[1] >= 0x80u && cursor[1] <= 0xbfu) {
        return 2u;
    }
    if (first >= 0xe0u && first <= 0xefu &&
        cursor[1] >= 0x80u && cursor[1] <= 0xbfu &&
        cursor[2] >= 0x80u && cursor[2] <= 0xbfu &&
        !(first == 0xe0u && cursor[1] < 0xa0u) &&
        !(first == 0xedu && cursor[1] >= 0xa0u)) {
        return 3u;
    }
    if (first >= 0xf0u && first <= 0xf4u &&
        cursor[1] >= 0x80u && cursor[1] <= 0xbfu &&
        cursor[2] >= 0x80u && cursor[2] <= 0xbfu &&
        cursor[3] >= 0x80u && cursor[3] <= 0xbfu &&
        !(first == 0xf0u && cursor[1] < 0x90u) &&
        !(first == 0xf4u && cursor[1] >= 0x90u)) {
        return 4u;
    }
    return 0u;
}

static void buffer_string(JsonBuffer *buffer, const char *text)
{
    static const char hex[] = "0123456789abcdef";
    const unsigned char *cursor = (const unsigned char *)text;
    buffer_append(buffer, "\"");
    while (!buffer->failed && *cursor != '\0') {
        unsigned char value = *cursor++;
        if (value == '"' || value == '\\') {
            char escaped[2] = {'\\', (char)value};
            buffer_append_n(buffer, escaped, sizeof(escaped));
        } else if (value >= 0x20u && value <= 0x7eu) {
            char plain = (char)value;
            buffer_append_n(buffer, &plain, 1u);
        } else if (value >= 0x80u) {
            size_t length = utf8_sequence_length(cursor - 1u);
            if (length > 0u) {
                buffer_append_n(buffer, (const char *)cursor - 1, length);
                cursor += length - 1u;
            } else {
                buffer_append(buffer, "\\ufffd");
            }
        } else {
            char escaped[6] = {
                '\\', 'u', '0', '0', hex[(value >> 4u) & 0x0fu], hex[value & 0x0fu]
            };
            buffer_append_n(buffer, escaped, sizeof(escaped));
        }
    }
    buffer_append(buffer, "\"");
}

static const char *root_path(const char *environment, const char *fallback)
{
    const char *value = getenv(environment);
    return value != NULL && *value != '\0' ? value : fallback;
}

static int valid_name(const char *name)
{
    size_t length = 0u;
    if (name == NULL || !isalnum((unsigned char)name[0])) {
        return 0;
    }
    while (name[length] != '\0') {
        unsigned char value = (unsigned char)name[length];
        if (!(isalnum(value) || value == '.' || value == '_' || value == '-')) {
            return 0;
        }
        if (++length > MAX_NAME) {
            return 0;
        }
    }
    return length > 0u;
}

static int join_path(char *output, size_t capacity, const char *root, const char *name, const char *file)
{
    int result;
    if (!valid_name(name)) {
        return 0;
    }
    result = file == NULL
        ? snprintf(output, capacity, "%s/%s", root, name)
        : snprintf(output, capacity, "%s/%s/%s", root, name, file);
    return result > 0 && (size_t)result < capacity;
}

static int wpa_control_path(const char *interface, char *path, size_t capacity)
{
    const char *root = root_path("MSYS_HAL_WPA_ROOT", "/run/wpa_supplicant");
    int result;
    if (!valid_name(interface)) {
        return 0;
    }
    result = snprintf(path, capacity, "%s/%s", root, interface);
    return result > 0 && (size_t)result < capacity;
}

static int wpa_available(const char *interface)
{
    char path[PATH_MAX];
    struct stat status;
    return wpa_control_path(interface, path, sizeof(path)) &&
           lstat(path, &status) == 0 && S_ISSOCK(status.st_mode);
}

static int wpa_request(
    const char *interface,
    const char *command,
    char *response,
    size_t capacity
)
{
    static unsigned int request_counter = 0u;
    struct sockaddr_un remote_address;
    struct sockaddr_un local_address;
    struct pollfd poll_descriptor;
    char path[PATH_MAX];
    size_t command_length;
    size_t local_length;
    ssize_t sent;
    ssize_t received;
    int descriptor;
    int flags;
    int result = 0;
    if (response == NULL || capacity < 2u || !wpa_available(interface) ||
        command == NULL || (command_length = strlen(command)) == 0u ||
        command_length > WPA_COMMAND_CAPACITY ||
        strchr(command, '\n') != NULL || strchr(command, '\r') != NULL ||
        !wpa_control_path(interface, path, sizeof(path)) ||
        strlen(path) >= sizeof(remote_address.sun_path)) {
        return 0;
    }
    descriptor = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (descriptor < 0) {
        return 0;
    }
    flags = fcntl(descriptor, F_GETFD);
    if (flags >= 0) {
        (void)fcntl(descriptor, F_SETFD, flags | FD_CLOEXEC);
    }
    memset(&local_address, 0, sizeof(local_address));
    local_address.sun_family = AF_UNIX;
    ++request_counter;
    local_length = (size_t)snprintf(
        local_address.sun_path + 1,
        sizeof(local_address.sun_path) - 1u,
        "msys-hal-%ld-%u",
        (long)getpid(),
        request_counter
    );
    if (local_length == 0u || local_length >= sizeof(local_address.sun_path) - 1u ||
        bind(
            descriptor,
            (const struct sockaddr *)&local_address,
            (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1u + local_length)
        ) != 0) {
        (void)close(descriptor);
        return 0;
    }
    memset(&remote_address, 0, sizeof(remote_address));
    remote_address.sun_family = AF_UNIX;
    memcpy(remote_address.sun_path, path, strlen(path) + 1u);
    if (connect(
            descriptor,
            (const struct sockaddr *)&remote_address,
            (socklen_t)sizeof(remote_address)
        ) != 0) {
        (void)close(descriptor);
        return 0;
    }
    sent = send(descriptor, command, command_length, 0);
    if (sent != (ssize_t)command_length) {
        (void)close(descriptor);
        return 0;
    }
    poll_descriptor.fd = descriptor;
    poll_descriptor.events = POLLIN;
    poll_descriptor.revents = 0;
    if (poll(&poll_descriptor, 1u, 1000) <= 0 ||
        (poll_descriptor.revents & POLLIN) == 0) {
        (void)close(descriptor);
        return 0;
    }
    received = recv(descriptor, response, capacity - 1u, 0);
    if (received > 0 && (size_t)received < capacity) {
        size_t length = (size_t)received;
        response[length] = '\0';
        while (length > 0u &&
               (response[length - 1u] == '\n' || response[length - 1u] == '\r')) {
            response[--length] = '\0';
        }
        result = length > 0u;
    }
    (void)close(descriptor);
    return result;
}

static int wpa_ok(const char *interface, const char *command)
{
    char response[32];
    return wpa_request(interface, command, response, sizeof(response)) &&
           strcmp(response, "OK") == 0;
}

static uint16_t read_le16(const unsigned char *value)
{
    return (uint16_t)((uint16_t)value[0] | ((uint16_t)value[1] << 8u));
}

static uint32_t read_le32(const unsigned char *value)
{
    return (uint32_t)value[0] |
           ((uint32_t)value[1] << 8u) |
           ((uint32_t)value[2] << 16u) |
           ((uint32_t)value[3] << 24u);
}

static void write_le16(unsigned char *output, uint16_t value)
{
    output[0] = (unsigned char)(value & 0xffu);
    output[1] = (unsigned char)((value >> 8u) & 0xffu);
}

static int bluetooth_index(const char *interface, int *index)
{
    int parsed;
    if (interface == NULL || strncmp(interface, "hci", 3u) != 0 ||
        !parse_decimal(interface + 3, 0, 65534, &parsed)) {
        return 0;
    }
    *index = parsed;
    return 1;
}

static int bluetooth_socket(void)
{
    MsysSockaddrHci address;
    int descriptor = socket(MSYS_AF_BLUETOOTH, SOCK_RAW, MSYS_BTPROTO_HCI);
    int flags;
    if (descriptor < 0) {
        bluetooth_error("socket", errno);
        return -1;
    }
    flags = fcntl(descriptor, F_GETFD);
    if (flags >= 0) {
        (void)fcntl(descriptor, F_SETFD, flags | FD_CLOEXEC);
    }
    memset(&address, 0, sizeof(address));
    address.family = MSYS_AF_BLUETOOTH;
    address.device = MSYS_HCI_DEV_NONE;
    address.channel = MSYS_HCI_CHANNEL_CONTROL;
    if (bind(
            descriptor,
            (const struct sockaddr *)&address,
            (socklen_t)sizeof(address)
        ) != 0) {
        bluetooth_error("bind", errno);
        (void)close(descriptor);
        return -1;
    }
    return descriptor;
}

static void bluetooth_address(const unsigned char value[6], char output[18])
{
    (void)snprintf(
        output,
        18u,
        "%02X:%02X:%02X:%02X:%02X:%02X",
        value[5], value[4], value[3], value[2], value[1], value[0]
    );
}

static void bluetooth_name_from_eir(
    const unsigned char *eir,
    size_t length,
    char output[64]
)
{
    size_t position = 0u;
    output[0] = '\0';
    while (position < length) {
        size_t field_length = eir[position];
        unsigned char type;
        size_t copy_length;
        size_t index;
        if (field_length == 0u) {
            break;
        }
        if (field_length + 1u > length - position) {
            return;
        }
        type = eir[position + 1u];
        if ((type == 0x08u || type == 0x09u) && field_length > 1u) {
            copy_length = field_length - 1u;
            if (copy_length >= 64u) {
                copy_length = 63u;
            }
            memcpy(output, eir + position + 2u, copy_length);
            output[copy_length] = '\0';
            for (index = 0u; index < copy_length; ++index) {
                unsigned char value = (unsigned char)output[index];
                if (value < 0x20u || value == 0x7fu) {
                    output[index] = ' ';
                }
            }
            if (type == 0x09u) {
                return;
            }
        }
        position += field_length + 1u;
    }
}

static void remember_bluetooth_found(const unsigned char *packet, size_t length)
{
    char address[18];
    char name[64];
    uint16_t eir_length;
    size_t index;
    if (length < 14u) {
        return;
    }
    eir_length = read_le16(packet + 12u);
    if ((size_t)eir_length > length - 14u) {
        return;
    }
    bluetooth_address(packet, address);
    bluetooth_name_from_eir(packet + 14u, eir_length, name);
    for (index = 0u; index < bluetooth_found_count; ++index) {
        if (strcmp(bluetooth_found[index].address, address) == 0) {
            bluetooth_found[index].rssi = (int)(int8_t)packet[7u];
            if (name[0] != '\0') {
                (void)snprintf(
                    bluetooth_found[index].name,
                    sizeof(bluetooth_found[index].name),
                    "%s",
                    name
                );
            }
            return;
        }
    }
    if (bluetooth_found_count >= MAX_BLUETOOTH_DISCOVERED) {
        return;
    }
    (void)snprintf(
        bluetooth_found[bluetooth_found_count].address,
        sizeof(bluetooth_found[bluetooth_found_count].address),
        "%s",
        address
    );
    (void)snprintf(
        bluetooth_found[bluetooth_found_count].name,
        sizeof(bluetooth_found[bluetooth_found_count].name),
        "%s",
        name
    );
    bluetooth_found[bluetooth_found_count].address_type = packet[6u];
    bluetooth_found[bluetooth_found_count].rssi = (int)(int8_t)packet[7u];
    ++bluetooth_found_count;
}

static int receive_mgmt_event(
    int descriptor,
    int timeout_ms,
    uint16_t expected_opcode,
    uint16_t expected_index,
    unsigned char *response,
    size_t response_capacity,
    size_t *response_length
)
{
    unsigned char packet[MGMT_PACKET_CAPACITY];
    struct pollfd poll_descriptor;
    ssize_t received;
    uint16_t event;
    uint16_t index;
    uint16_t payload_length;
    poll_descriptor.fd = descriptor;
    poll_descriptor.events = POLLIN;
    poll_descriptor.revents = 0;
    if (poll(&poll_descriptor, 1u, timeout_ms) <= 0 ||
        (poll_descriptor.revents & POLLIN) == 0) {
        bluetooth_error("poll", errno);
        return 0;
    }
    received = recv(descriptor, packet, sizeof(packet), 0);
    if (received < 6) {
        bluetooth_error("recv", received < 0 ? errno : (int)received);
        return -1;
    }
    event = read_le16(packet);
    index = read_le16(packet + 2u);
    payload_length = read_le16(packet + 4u);
    if ((size_t)payload_length != (size_t)received - 6u) {
        bluetooth_error("length", payload_length);
        return -1;
    }
    if (event == MGMT_EV_DEVICE_FOUND && index == expected_index) {
        remember_bluetooth_found(packet + 6u, payload_length);
        return 2;
    }
    if (index != expected_index || payload_length < 3u ||
        (event != MGMT_EV_CMD_COMPLETE && event != MGMT_EV_CMD_STATUS) ||
        read_le16(packet + 6u) != expected_opcode) {
        return 2;
    }
    if (packet[8u] != 0u) {
        bluetooth_error("status", packet[8u]);
        return -1;
    }
    if (event == MGMT_EV_CMD_COMPLETE) {
        size_t data_length = (size_t)payload_length - 3u;
        /* Some successful commands, notably Set Powered, always return the
         * updated settings word.  Callers that pass response == NULL are
         * explicitly discarding that bounded response, not advertising a
         * zero-byte receive buffer. */
        if (response != NULL && data_length > response_capacity) {
            return -1;
        }
        if (response != NULL && data_length > 0u) {
            memcpy(response, packet + 9u, data_length);
        }
        if (response_length != NULL) {
            *response_length = data_length;
        }
    } else if (response_length != NULL) {
        *response_length = 0u;
    }
    return 1;
}

static int mgmt_command(
    int descriptor,
    uint16_t opcode,
    uint16_t index,
    const unsigned char *payload,
    size_t payload_length,
    unsigned char *response,
    size_t response_capacity,
    size_t *response_length
)
{
    unsigned char packet[64];
    size_t packet_length = payload_length + 6u;
    ssize_t sent;
    int attempts;
    if (payload_length > sizeof(packet) - 6u) {
        return 0;
    }
    write_le16(packet, opcode);
    write_le16(packet + 2u, index);
    write_le16(packet + 4u, (uint16_t)payload_length);
    if (payload_length > 0u) {
        memcpy(packet + 6u, payload, payload_length);
    }
    sent = write(descriptor, packet, packet_length);
    /* The kernel management control channel returns zero after consuming a
     * complete command on several 5.x kernels; negative is the only failure. */
    if (sent < 0) {
        bluetooth_error("send", sent < 0 ? errno : (int)sent);
        return 0;
    }
    for (attempts = 0; attempts < 12; ++attempts) {
        int result = receive_mgmt_event(
            descriptor,
            250,
            opcode,
            index,
            response,
            response_capacity,
            response_length
        );
        if (result == 1) {
            return 1;
        }
        if (result < 0) {
            return 0;
        }
    }
    return 0;
}

static int mgmt_controller_index(int descriptor, int preferred, int *selected)
{
    unsigned char response[2u + MAX_ENTRIES * 2u];
    size_t response_length = 0u;
    uint16_t count;
    size_t index;
    if (!mgmt_command(
            descriptor,
            MGMT_OP_READ_INDEX_LIST,
            MSYS_HCI_DEV_NONE,
            NULL,
            0u,
            response,
            sizeof(response),
            &response_length
        ) || response_length < 2u) {
        return 0;
    }
    count = read_le16(response);
    if (count == 0u || count > MAX_ENTRIES ||
        response_length < 2u + (size_t)count * 2u) {
        bluetooth_error("index-list", count);
        return 0;
    }
    for (index = 0u; index < count; ++index) {
        int candidate = read_le16(response + 2u + index * 2u);
        if (candidate == preferred) {
            *selected = candidate;
            return 1;
        }
    }
    if (count == 1u) {
        *selected = read_le16(response + 2u);
        return 1;
    }
    bluetooth_error("index-missing", preferred);
    return 0;
}

static int bluetooth_info(const char *interface, BluetoothInfo *info)
{
    unsigned char response[512];
    size_t response_length = 0u;
    int descriptor;
    int preferred;
    int index;
    size_t name_length;
    if (info == NULL || !bluetooth_index(interface, &preferred) ||
        (descriptor = bluetooth_socket()) < 0) {
        return 0;
    }
    if (!mgmt_controller_index(descriptor, preferred, &index) ||
        !mgmt_command(
            descriptor,
            MGMT_OP_READ_INFO,
            (uint16_t)index,
            NULL,
            0u,
            response,
            sizeof(response),
            &response_length
        ) || response_length < 20u) {
        if (response_length < 20u && strcmp(bluetooth_management_error, "not-probed") == 0) {
            bluetooth_error("read-info", (int)response_length);
        }
        (void)close(descriptor);
        return 0;
    }
    memset(info, 0, sizeof(*info));
    info->index = index;
    bluetooth_address(response, info->address);
    info->powered = (read_le32(response + 13u) & MGMT_SETTING_POWERED) != 0u;
    info->discoverable = (read_le32(response + 13u) & MGMT_SETTING_DISCOVERABLE) != 0u;
    name_length = strnlen((const char *)response + 20u, response_length - 20u);
    if (name_length >= sizeof(info->name)) {
        name_length = sizeof(info->name) - 1u;
    }
    memcpy(info->name, response + 20u, name_length);
    info->name[name_length] = '\0';
    (void)snprintf(
        bluetooth_management_error,
        sizeof(bluetooth_management_error),
        "%s",
        "none"
    );
    (void)close(descriptor);
    return 1;
}

static int set_bluetooth_power(const char *interface, int powered)
{
    unsigned char requested = powered ? 1u : 0u;
    size_t response_length = 0u;
    int descriptor;
    BluetoothInfo info;
    int result;
    if (!bluetooth_info(interface, &info) || (descriptor = bluetooth_socket()) < 0) {
        return 0;
    }
    result = mgmt_command(
        descriptor,
        MGMT_OP_SET_POWERED,
        (uint16_t)info.index,
        &requested,
        1u,
        NULL,
        0u,
        &response_length
    );
    (void)close(descriptor);
    return result;
}

static int scan_bluetooth_devices(const char *interface)
{
    unsigned char type = MGMT_DISCOVERY_ALL;
    struct timespec start;
    struct timespec now;
    size_t response_length = 0u;
    BluetoothInfo info;
    int descriptor;
    int elapsed_ms = 0;
    if (!bluetooth_info(interface, &info) || !info.powered ||
        (descriptor = bluetooth_socket()) < 0) {
        return 0;
    }
    bluetooth_found_count = 0u;
    if (!mgmt_command(
            descriptor,
            MGMT_OP_START_DISCOVERY,
            (uint16_t)info.index,
            &type,
            1u,
            NULL,
            0u,
            &response_length
        )) {
        (void)close(descriptor);
        return 0;
    }
    (void)clock_gettime(CLOCK_MONOTONIC, &start);
    while (elapsed_ms < 1800) {
        (void)receive_mgmt_event(
            descriptor,
            180,
            0xffffu,
            (uint16_t)info.index,
            NULL,
            0u,
            NULL
        );
        (void)clock_gettime(CLOCK_MONOTONIC, &now);
        elapsed_ms = (int)((now.tv_sec - start.tv_sec) * 1000L +
            (now.tv_nsec - start.tv_nsec) / 1000000L);
    }
    (void)mgmt_command(
        descriptor,
        MGMT_OP_STOP_DISCOVERY,
        (uint16_t)info.index,
        &type,
        1u,
        NULL,
        0u,
        &response_length
    );
    (void)close(descriptor);
    return 1;
}

static int read_text_file(const char *path, char *output, size_t capacity)
{
    int descriptor;
    ssize_t received;
    size_t length;
    char *cursor;
    descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) {
        return 0;
    }
    received = read(descriptor, output, capacity);
    (void)close(descriptor);
    if (received <= 0 || (size_t)received >= capacity) {
        return 0;
    }
    length = (size_t)received;
    while (length > 0u && isspace((unsigned char)output[length - 1u]) != 0) {
        --length;
    }
    output[length] = '\0';
    cursor = output;
    while (*cursor != '\0') {
        unsigned char value = (unsigned char)*cursor++;
        if (value < 0x20u || value > 0x7eu) {
            return 0;
        }
    }
    return 1;
}

static int read_i64_file(const char *path, int64_t minimum, int64_t maximum, int64_t *value)
{
    char text[64];
    char *end = NULL;
    long long parsed;
    if (!read_text_file(path, text, sizeof(text))) {
        return 0;
    }
    errno = 0;
    parsed = strtoll(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed < minimum || parsed > maximum) {
        return 0;
    }
    *value = (int64_t)parsed;
    return 1;
}

static int write_i64_verified(const char *path, int64_t requested)
{
    char text[64];
    int descriptor;
    int length = snprintf(text, sizeof(text), "%" PRId64 "\n", requested);
    int64_t observed;
    ssize_t written;
    if (length <= 0 || (size_t)length >= sizeof(text)) {
        return 0;
    }
    descriptor = open(path, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) {
        return 0;
    }
    written = write(descriptor, text, (size_t)length);
    if (close(descriptor) != 0 || written != length) {
        return 0;
    }
    return read_i64_file(path, INT64_MIN, INT64_MAX, &observed) && observed == requested;
}

typedef struct {
    char name[MAX_NAME + 1];
    int hard_blocked;
    int unblocked;
    int writable;
} RadioPower;

static int radio_power(const char *domain, RadioPower *result)
{
    const char *root = root_path("MSYS_HAL_RFKILL_ROOT", "/sys/class/rfkill");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, "rfkill", names);
    size_t index;
    if (result == NULL ||
        (strcmp(domain, "network") != 0 && strcmp(domain, "bluetooth") != 0)) {
        return 0;
    }
    memset(result, 0, sizeof(*result));
    for (index = 0u; index < count; ++index) {
        char path[PATH_MAX];
        char type[32];
        int64_t hard;
        int64_t soft;
        int matches;
        if (!join_path(path, sizeof(path), root, names[index], "type") ||
            !read_text_file(path, type, sizeof(type))) {
            continue;
        }
        matches = strcmp(domain, "bluetooth") == 0
            ? strcmp(type, "bluetooth") == 0
            : (strcmp(type, "wlan") == 0 || strcmp(type, "wifi") == 0);
        if (!matches ||
            !join_path(path, sizeof(path), root, names[index], "hard") ||
            !read_i64_file(path, 0, 1, &hard) ||
            !join_path(path, sizeof(path), root, names[index], "soft") ||
            !read_i64_file(path, 0, 1, &soft)) {
            continue;
        }
        memcpy(result->name, names[index], strlen(names[index]) + 1u);
        result->hard_blocked = hard != 0;
        result->unblocked = soft == 0;
        result->writable = !result->hard_blocked && access(path, W_OK) == 0;
        return 1;
    }
    return 0;
}

static int set_radio_power(const char *domain, int powered)
{
    const char *root = root_path("MSYS_HAL_RFKILL_ROOT", "/sys/class/rfkill");
    RadioPower state;
    char path[PATH_MAX];
    if (!radio_power(domain, &state) || state.hard_blocked || !state.writable ||
        !join_path(path, sizeof(path), root, state.name, "soft")) {
        return 0;
    }
    return write_i64_verified(path, powered ? 0 : 1);
}

typedef struct {
    int (*read_info)(const char *interface, BluetoothInfo *info);
    int (*write_management)(const char *interface, int powered);
    int (*read_rfkill)(const char *domain, RadioPower *state);
    int (*write_rfkill)(const char *domain, int unblocked);
    void (*wait_ms)(unsigned int milliseconds);
} BluetoothPowerOps;

static int bluetooth_management_index_missing(void)
{
    return strcmp(bluetooth_management_error, "index-list:0") == 0;
}

static void bluetooth_wait_ms(unsigned int milliseconds)
{
    struct timespec delay;
    delay.tv_sec = (time_t)(milliseconds / 1000u);
    delay.tv_nsec = (long)(milliseconds % 1000u) * 1000000L;
    while (nanosleep(&delay, &delay) != 0 && errno == EINTR) {
    }
}

static int request_bluetooth_power_with(
    const char *interface,
    int powered,
    const BluetoothPowerOps *ops
)
{
    BluetoothInfo info;
    RadioPower radio;
    int attempt;

    if (interface == NULL || ops == NULL || ops->read_info == NULL ||
        ops->write_management == NULL || ops->read_rfkill == NULL ||
        ops->write_rfkill == NULL || ops->wait_ms == NULL) {
        return 0;
    }

    if (ops->read_info(interface, &info)) {
        if (info.powered == powered) {
            return 1;
        }
        if (!ops->write_management(interface, powered)) {
            return 0;
        }
        if (!powered) {
            /* Qualcomm WCNSS unregisters its Management index after a
             * successful power-off.  Either an observable unpowered
             * controller or that exact absence is a verified off state. */
            if (ops->read_info(interface, &info)) {
                return !info.powered;
            }
            return bluetooth_management_index_missing();
        }
        for (attempt = 0; attempt < 10; ++attempt) {
            if (ops->read_info(interface, &info)) {
                return info.powered;
            }
            ops->wait_ms(50u);
        }
        return 0;
    }

    if (!bluetooth_management_index_missing()) {
        return 0;
    }
    if (!powered) {
        /* No registered controller is an actual off state.  rfkill soft=0
         * means only “not blocked” and must not override this fact. */
        return 1;
    }
    if (!ops->read_rfkill("bluetooth", &radio) ||
        radio.hard_blocked || !radio.writable) {
        return 0;
    }

    /* An already-unblocked WCNSS with no Management index needs an edge, not
     * another idempotent soft=0 write.  Bound the pulse and re-probe window. */
    if (radio.unblocked) {
        if (!ops->write_rfkill("bluetooth", 0)) {
            return 0;
        }
        ops->wait_ms(100u);
    }
    if (!ops->write_rfkill("bluetooth", 1)) {
        return 0;
    }
    /* Let the kernel's rfkill callback run hci_power_on.  A legacy HCIDEVUP
     * during HCI_SETUP initializes address/features without clearing the
     * setup flag, which permanently hides this controller from MGMT. */
    for (attempt = 0; attempt < 50; ++attempt) {
        if (ops->read_info(interface, &info)) {
            if (info.powered) {
                return 1;
            }
            if (!ops->write_management(interface, 1)) {
                return 0;
            }
        } else {
            if (!bluetooth_management_index_missing()) {
                return 0;
            }
        }
        ops->wait_ms(100u);
    }
    return 0;
}

static int request_bluetooth_power(const char *interface, int powered)
{
    static const BluetoothPowerOps operations = {
        bluetooth_info,
        set_bluetooth_power,
        radio_power,
        set_radio_power,
        bluetooth_wait_ms,
    };
    return request_bluetooth_power_with(interface, powered, &operations);
}

static int compare_names(const void *left, const void *right)
{
    return strcmp((const char *)left, (const char *)right);
}

static size_t list_entries(
    const char *root,
    const char *prefix,
    char names[MAX_ENTRIES][MAX_NAME + 1]
)
{
    DIR *directory;
    struct dirent *entry;
    size_t count = 0u;
    directory = opendir(root);
    if (directory == NULL) {
        return 0u;
    }
    while (count < MAX_ENTRIES && (entry = readdir(directory)) != NULL) {
        char path[PATH_MAX];
        struct stat status;
        if (!valid_name(entry->d_name) ||
            (prefix != NULL && strncmp(entry->d_name, prefix, strlen(prefix)) != 0) ||
            !join_path(path, sizeof(path), root, entry->d_name, NULL) ||
            stat(path, &status) != 0 || !S_ISDIR(status.st_mode)) {
            continue;
        }
        memcpy(names[count], entry->d_name, strlen(entry->d_name) + 1u);
        ++count;
    }
    (void)closedir(directory);
    qsort(names, count, sizeof(names[0]), compare_names);
    return count;
}

static void add_device(
    DeviceList *devices,
    DeviceKind kind,
    const char *domain,
    const char *name,
    const char *label,
    const char *detail,
    int64_t maximum,
    int mutable
)
{
    Device *device;
    if (devices->count >= MAX_DEVICES || !valid_name(name)) {
        return;
    }
    device = &devices->items[devices->count++];
    memset(device, 0, sizeof(*device));
    device->kind = kind;
    (void)snprintf(device->domain, sizeof(device->domain), "%s", domain);
    (void)snprintf(device->name, sizeof(device->name), "%s", name);
    (void)snprintf(device->label, sizeof(device->label), "%s", label != NULL ? label : name);
    (void)snprintf(device->detail, sizeof(device->detail), "%s", detail != NULL ? detail : "unknown");
    device->maximum = maximum;
    device->mutable = mutable;
}

static int device_compare(const void *left, const void *right)
{
    const Device *a = (const Device *)left;
    const Device *b = (const Device *)right;
    int domain_result = strcmp(a->domain, b->domain);
    return domain_result != 0 ? domain_result : strcmp(a->name, b->name);
}

static void scan_power(DeviceList *devices)
{
    const char *root = root_path("MSYS_HAL_POWER_ROOT", "/sys/class/power_supply");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, NULL, names);
    size_t index;
    for (index = 0u; index < count; ++index) {
        char path[PATH_MAX];
        char type[MAX_NAME + 1] = "unknown";
        if (join_path(path, sizeof(path), root, names[index], "type")) {
            (void)read_text_file(path, type, sizeof(type));
        }
        add_device(devices, DEVICE_POWER, "power", names[index], names[index], type, 0, 0);
    }
}

static void scan_thermal(DeviceList *devices)
{
    const char *root = root_path("MSYS_HAL_THERMAL_ROOT", "/sys/class/thermal");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, "thermal_zone", names);
    size_t index;
    for (index = 0u; index < count; ++index) {
        char path[PATH_MAX];
        char type[MAX_NAME + 1];
        memcpy(type, names[index], strlen(names[index]) + 1u);
        if (join_path(path, sizeof(path), root, names[index], "type")) {
            (void)read_text_file(path, type, sizeof(type));
        }
        add_device(devices, DEVICE_THERMAL, "thermal", names[index], type, type, 0, 0);
    }
}

static void scan_backlight(DeviceList *devices)
{
    const char *root = root_path("MSYS_HAL_BACKLIGHT_ROOT", "/sys/class/backlight");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, NULL, names);
    size_t index;
    for (index = 0u; index < count; ++index) {
        char maximum_path[PATH_MAX];
        char brightness_path[PATH_MAX];
        int64_t maximum;
        int64_t brightness;
        if (!join_path(maximum_path, sizeof(maximum_path), root, names[index], "max_brightness") ||
            !join_path(brightness_path, sizeof(brightness_path), root, names[index], "brightness") ||
            !read_i64_file(maximum_path, 1, INT32_MAX, &maximum) ||
            !read_i64_file(brightness_path, 0, maximum, &brightness)) {
            continue;
        }
        add_device(
            devices,
            DEVICE_BACKLIGHT,
            "backlight",
            names[index],
            names[index],
            "backlight",
            maximum,
            access(brightness_path, W_OK) == 0
        );
    }
}

static void scan_input(DeviceList *devices)
{
    const char *root = root_path("MSYS_HAL_NATIVE_INPUT_ROOT", "/sys/class/input");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, "event", names);
    size_t index;
    for (index = 0u; index < count; ++index) {
        add_device(devices, DEVICE_INPUT, "input", names[index], names[index], "input-event", 0, 0);
    }
}

static void scan_network(DeviceList *devices)
{
    const char *root = root_path("MSYS_HAL_NETWORK_ROOT", "/sys/class/net");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, NULL, names);
    size_t index;
    RadioPower power;
    int has_power = radio_power("network", &power);
    for (index = 0u; index < count; ++index) {
        char wireless[PATH_MAX];
        struct stat status;
        const char *kind = strcmp(names[index], "lo") == 0 ? "loopback" : "ethernet";
        int mutable = MUTABLE_NONE;
        if (join_path(wireless, sizeof(wireless), root, names[index], "wireless") &&
            stat(wireless, &status) == 0 && S_ISDIR(status.st_mode)) {
            kind = "wifi";
            if (wpa_available(names[index])) {
                mutable |= MUTABLE_ACTION;
            }
            if (has_power && power.writable) {
                mutable |= MUTABLE_STATE;
            }
        }
        add_device(
            devices,
            DEVICE_NETWORK,
            "network",
            names[index],
            names[index],
            kind,
            0,
            mutable
        );
    }
}

static void scan_bluetooth(DeviceList *devices)
{
    const char *root = root_path("MSYS_HAL_BLUETOOTH_ROOT", "/sys/class/bluetooth");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, "hci", names);
    size_t index;
    RadioPower power;
    int rfkill_mutable = radio_power("bluetooth", &power) && power.writable;
    for (index = 0u; index < count; ++index) {
        BluetoothInfo info;
        int mutable = rfkill_mutable ? MUTABLE_STATE : MUTABLE_NONE;
        if (bluetooth_info(names[index], &info)) {
            mutable |= MUTABLE_STATE | MUTABLE_ACTION;
        }
        add_device(
            devices,
            DEVICE_BLUETOOTH,
            "bluetooth",
            names[index],
            names[index],
            "controller",
            0,
            mutable
        );
    }
}

static void scan_rfkill(DeviceList *devices)
{
    const char *root = root_path("MSYS_HAL_RFKILL_ROOT", "/sys/class/rfkill");
    char names[MAX_ENTRIES][MAX_NAME + 1];
    size_t count = list_entries(root, "rfkill", names);
    size_t index;
    for (index = 0u; index < count; ++index) {
        char path[PATH_MAX];
        char type[32];
        char label[MAX_NAME + 1];
        int64_t hard = 1;
        int64_t soft;
        DeviceKind kind;
        const char *domain;
        if (!join_path(path, sizeof(path), root, names[index], "type") ||
            !read_text_file(path, type, sizeof(type))) {
            continue;
        }
        if (strcmp(type, "bluetooth") == 0) {
            kind = DEVICE_RFKILL_BLUETOOTH;
            domain = "bluetooth";
        } else if (strcmp(type, "wlan") == 0 || strcmp(type, "wifi") == 0) {
            kind = DEVICE_RFKILL_NETWORK;
            domain = "network";
        } else {
            continue;
        }
        memcpy(label, names[index], strlen(names[index]) + 1u);
        if (join_path(path, sizeof(path), root, names[index], "name")) {
            (void)read_text_file(path, label, sizeof(label));
        }
        if (join_path(path, sizeof(path), root, names[index], "hard")) {
            (void)read_i64_file(path, 0, 1, &hard);
        }
        if (!join_path(path, sizeof(path), root, names[index], "soft") ||
            !read_i64_file(path, 0, 1, &soft)) {
            continue;
        }
        add_device(
            devices,
            kind,
            domain,
            names[index],
            label,
            "rfkill",
            1,
            hard == 0 && access(path, W_OK) == 0 ? MUTABLE_STATE : MUTABLE_NONE
        );
    }
}

static void scan_devices(DeviceList *devices)
{
    memset(devices, 0, sizeof(*devices));
    scan_power(devices);
    scan_thermal(devices);
    scan_backlight(devices);
    scan_input(devices);
    scan_network(devices);
    scan_bluetooth(devices);
    scan_rfkill(devices);
    qsort(devices->items, devices->count, sizeof(devices->items[0]), device_compare);
}

static int domain_index(const char *domain)
{
    int index;
    for (index = 0; index < DOMAIN_COUNT; ++index) {
        if (strcmp(domain, DOMAINS[index]) == 0) {
            return index;
        }
    }
    return -1;
}

static int parse_payload(
    const char *payload,
    size_t length,
    JsonToken tokens[MAX_TOKENS],
    int *count
)
{
    *count = parse_json(payload, length, tokens, MAX_TOKENS);
    return *count > 0 && tokens[0].type == JT_OBJECT;
}

static int parse_inventory_request(
    const char *json,
    const JsonToken *tokens,
    int count,
    int selected[DOMAIN_COUNT]
)
{
    static const char *const allowed[] = {"domains", "refresh"};
    int domains;
    int refresh;
    int index;
    for (index = 0; index < DOMAIN_COUNT; ++index) {
        selected[index] = 1;
    }
    if (!object_validate_fields(json, tokens, count, 0, allowed, 2u)) {
        return 0;
    }
    refresh = object_field(json, tokens, count, 0, "refresh");
    if (refresh == -2 || (refresh >= 0 && !token_bool(json, &tokens[refresh], &index))) {
        return 0;
    }
    domains = object_field(json, tokens, count, 0, "domains");
    if (domains == -2) {
        return 0;
    }
    if (domains >= 0) {
        int cursor;
        int seen = 0;
        if (tokens[domains].type != JT_ARRAY) {
            return 0;
        }
        for (index = 0; index < DOMAIN_COUNT; ++index) {
            selected[index] = 0;
        }
        cursor = domains + 1;
        while (cursor < count && tokens[cursor].start < tokens[domains].end) {
            char name[32];
            int parsed_domain;
            if (tokens[cursor].parent != domains ||
                !copy_string(json, &tokens[cursor], name, sizeof(name))) {
                return 0;
            }
            parsed_domain = domain_index(name);
            if (parsed_domain < 0 || selected[parsed_domain]) {
                return 0;
            }
            selected[parsed_domain] = 1;
            ++seen;
            cursor = token_next(tokens, count, cursor);
        }
        if (seen == 0 || seen > DOMAIN_COUNT) {
            return 0;
        }
    }
    return 1;
}

static void append_mutable(JsonBuffer *buffer, const Device *device)
{
    int first = 1;
    buffer_append(buffer, "[");
    if ((device->mutable & MUTABLE_STATE) != 0) {
        if (device->kind == DEVICE_BACKLIGHT) {
            buffer_append(buffer, "\"brightness\",\"brightness_percent\"");
            first = 0;
        } else if (device->kind == DEVICE_NETWORK ||
                   device->kind == DEVICE_BLUETOOTH ||
                   device->kind == DEVICE_RFKILL_NETWORK ||
                   device->kind == DEVICE_RFKILL_BLUETOOTH) {
            buffer_append(buffer, "\"powered\"");
            first = 0;
        }
    }
    if ((device->mutable & MUTABLE_ACTION) != 0) {
        if (!first) {
            buffer_append(buffer, ",");
        }
        buffer_append(buffer, "\"action\"");
    }
    buffer_append(buffer, "]");
}

static void append_device_id(JsonBuffer *buffer, const Device *device);

static void append_inventory_device(JsonBuffer *buffer, const Device *device)
{
    buffer_append(buffer, "{\"id\":");
    append_device_id(buffer, device);
    buffer_append(buffer, ",\"domain\":");
    buffer_string(buffer, device->domain);
    buffer_append(buffer, ",\"name\":");
    buffer_string(buffer, device->label);
    buffer_append(buffer, ",\"available\":true,\"mutable\":");
    append_mutable(buffer, device);
    buffer_append(buffer, ",\"metadata\":{");
    switch (device->kind) {
    case DEVICE_POWER:
    case DEVICE_THERMAL:
        buffer_append(buffer, "\"type\":");
        buffer_string(buffer, device->detail);
        break;
    case DEVICE_BACKLIGHT:
        buffer_format(
            buffer,
            "\"type\":\"backlight\",\"max_brightness\":%" PRId64 ",\"control\":\"%s\"",
            device->maximum,
            device->mutable ? "writable" : "read-only"
        );
        break;
    case DEVICE_INPUT:
        buffer_append(buffer, "\"type\":\"input-event\"");
        break;
    case DEVICE_NETWORK:
        buffer_append(buffer, "\"kind\":");
        buffer_string(buffer, device->detail);
        if (strcmp(device->detail, "wifi") == 0) {
            buffer_append(buffer, ",\"wifi_control\":\"");
            buffer_append(
                buffer,
                (device->mutable & MUTABLE_ACTION) != 0 ? "available" : "unavailable"
            );
            buffer_append(buffer, "\"");
            if ((device->mutable & MUTABLE_ACTION) == 0) {
                buffer_append(
                    buffer,
                    ",\"wifi_control_reason\":"
                    "\"wpa-supplicant-control-unavailable\""
                );
            }
        }
        break;
    case DEVICE_BLUETOOTH:
        buffer_append(buffer, "\"kind\":\"controller\",\"management_control\":");
        buffer_string(
            buffer,
            (device->mutable & MUTABLE_ACTION) != 0 ? "available" : "unavailable"
        );
        buffer_append(buffer, ",\"discovery_control\":");
        buffer_string(
            buffer,
            (device->mutable & MUTABLE_ACTION) != 0 ? "available" : "unavailable"
        );
        buffer_append(
            buffer,
            ",\"pairing_control\":\"unsupported\","
            "\"pairing_reason\":\"pairing-not-supported\""
        );
        if ((device->mutable & MUTABLE_ACTION) == 0) {
            buffer_append(
                buffer,
                bluetooth_management_index_missing()
                    ? ",\"management_reason\":\"controller-not-registered\""
                    : ",\"management_reason\":"
                      "\"linux-management-control-unavailable\""
            );
            buffer_append(buffer, ",\"power_control\":");
            buffer_string(
                buffer,
                (device->mutable & MUTABLE_STATE) != 0
                    ? (bluetooth_management_index_missing()
                        ? "rfkill-reprobe"
                        : "rfkill")
                    : "read-only"
            );
        }
        break;
    case DEVICE_RFKILL_NETWORK:
    case DEVICE_RFKILL_BLUETOOTH:
        buffer_append(buffer, "\"kind\":\"rfkill\"");
        break;
    }
    buffer_append(buffer, "}}");
}

static void append_device_id(JsonBuffer *buffer, const Device *device)
{
    char identifier[24 + MAX_NAME + 2];
    (void)snprintf(identifier, sizeof(identifier), "%s:%s", device->domain, device->name);
    buffer_string(buffer, identifier);
}

static int build_inventory(
    const char *json,
    const JsonToken *tokens,
    int token_count,
    JsonBuffer *buffer,
    size_t *device_count
)
{
    int selected[DOMAIN_COUNT];
    size_t counts[DOMAIN_COUNT] = {0u};
    DeviceList devices;
    size_t index;
    int domain;
    int first;
    if (!parse_inventory_request(json, tokens, token_count, selected)) {
        return 0;
    }
    scan_devices(&devices);
    for (index = 0u; index < devices.count; ++index) {
        int item_domain = domain_index(devices.items[index].domain);
        if (item_domain >= 0 && selected[item_domain]) {
            ++counts[item_domain];
        }
    }
    buffer_format(
        buffer,
        "{\"schema\":\"%s\",\"revision\":%" PRIu64 ",\"domains\":[",
        MANAGER_SCHEMA,
        revision_number
    );
    first = 1;
    for (domain = 0; domain < DOMAIN_COUNT; ++domain) {
        if (!selected[domain]) {
            continue;
        }
        if (!first) {
            buffer_append(buffer, ",");
        }
        first = 0;
        buffer_append(buffer, "{\"domain\":");
        buffer_string(buffer, DOMAINS[domain]);
        if (counts[domain] > 0u) {
            buffer_append(buffer, ",\"status\":\"available\",\"selection\":\"automatic\",\"provider\":\"");
            buffer_append(buffer, COMPONENT_ID);
            buffer_append(buffer, "\"}");
        } else {
            buffer_append(buffer, ",\"status\":\"unavailable\",\"reason\":\"no-device\",\"selection\":\"automatic\",\"provider\":null}");
        }
    }
    buffer_append(buffer, "],\"devices\":[");
    first = 1;
    *device_count = 0u;
    for (index = 0u; index < devices.count; ++index) {
        int item_domain = domain_index(devices.items[index].domain);
        if (item_domain < 0 || !selected[item_domain]) {
            continue;
        }
        if (!first) {
            buffer_append(buffer, ",");
        }
        first = 0;
        append_inventory_device(buffer, &devices.items[index]);
        ++*device_count;
    }
    buffer_append(buffer, "]}");
    return !buffer->failed;
}

static const Device *find_device(DeviceList *devices, const char *identifier)
{
    const char *separator = strchr(identifier, ':');
    size_t domain_length;
    size_t index;
    if (separator == NULL || separator == identifier || !valid_name(separator + 1)) {
        return NULL;
    }
    domain_length = (size_t)(separator - identifier);
    for (index = 0u; index < devices->count; ++index) {
        const Device *device = &devices->items[index];
        if (strlen(device->domain) == domain_length &&
            memcmp(device->domain, identifier, domain_length) == 0 &&
            strcmp(device->name, separator + 1) == 0) {
            return device;
        }
    }
    return NULL;
}

static const char *device_root(const Device *device)
{
    switch (device->kind) {
    case DEVICE_POWER:
        return root_path("MSYS_HAL_POWER_ROOT", "/sys/class/power_supply");
    case DEVICE_THERMAL:
        return root_path("MSYS_HAL_THERMAL_ROOT", "/sys/class/thermal");
    case DEVICE_BACKLIGHT:
        return root_path("MSYS_HAL_BACKLIGHT_ROOT", "/sys/class/backlight");
    case DEVICE_INPUT:
        return root_path("MSYS_HAL_NATIVE_INPUT_ROOT", "/sys/class/input");
    case DEVICE_NETWORK:
        return root_path("MSYS_HAL_NETWORK_ROOT", "/sys/class/net");
    case DEVICE_BLUETOOTH:
        return root_path("MSYS_HAL_BLUETOOTH_ROOT", "/sys/class/bluetooth");
    case DEVICE_RFKILL_NETWORK:
    case DEVICE_RFKILL_BLUETOOTH:
        return root_path("MSYS_HAL_RFKILL_ROOT", "/sys/class/rfkill");
    }
    return "";
}

static void append_text_value(
    JsonBuffer *buffer,
    const Device *device,
    const char *filename,
    const char *key,
    int *first
)
{
    char path[PATH_MAX];
    char value[MAX_NAME + 1];
    if (!join_path(path, sizeof(path), device_root(device), device->name, filename) ||
        !read_text_file(path, value, sizeof(value))) {
        return;
    }
    if (!*first) {
        buffer_append(buffer, ",");
    }
    *first = 0;
    buffer_string(buffer, key);
    buffer_append(buffer, ":");
    buffer_string(buffer, value);
}

static void append_integer_value(
    JsonBuffer *buffer,
    const Device *device,
    const char *filename,
    const char *key,
    int64_t minimum,
    int64_t maximum,
    int boolean_value,
    int *first
)
{
    char path[PATH_MAX];
    int64_t value;
    if (!join_path(path, sizeof(path), device_root(device), device->name, filename) ||
        !read_i64_file(path, minimum, maximum, &value)) {
        return;
    }
    if (!*first) {
        buffer_append(buffer, ",");
    }
    *first = 0;
    buffer_string(buffer, key);
    buffer_append(buffer, ":");
    if (boolean_value) {
        buffer_append(buffer, value != 0 ? "true" : "false");
    } else {
        buffer_format(buffer, "%" PRId64, value);
    }
}

static int copy_wpa_text(
    const char *start,
    size_t length,
    char *output,
    size_t capacity,
    int allow_empty
)
{
    size_t position = 0u;
    if (start == NULL || output == NULL || length >= capacity ||
        (!allow_empty && length == 0u)) {
        return 0;
    }
    memcpy(output, start, length);
    output[length] = '\0';
    while (position < length) {
        const unsigned char *cursor = (const unsigned char *)output + position;
        size_t sequence;
        if (*cursor < 0x20u || *cursor == 0x7fu) {
            return 0;
        }
        sequence = utf8_sequence_length(cursor);
        if (sequence == 0u || sequence > length - position) {
            return 0;
        }
        position += sequence;
    }
    return 1;
}

static int parse_decimal(const char *text, int minimum, int maximum, int *value)
{
    char *end = NULL;
    long parsed;
    if (text == NULL || *text == '\0') {
        return 0;
    }
    errno = 0;
    parsed = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' ||
        parsed < minimum || parsed > maximum) {
        return 0;
    }
    *value = (int)parsed;
    return 1;
}

static void append_wpa_status(JsonBuffer *buffer, const char *response)
{
    static const char *const allowed[] = {
        "bssid", "freq", "id", "ip_address", "key_mgmt", "mode", "ssid", "wpa_state"
    };
    const char *cursor = response;
    int first = 1;
    buffer_append(buffer, "{");
    while (*cursor != '\0') {
        const char *end = strchr(cursor, '\n');
        const char *equal;
        size_t line_length = end == NULL ? strlen(cursor) : (size_t)(end - cursor);
        equal = memchr(cursor, '=', line_length);
        if (equal != NULL && equal > cursor) {
            size_t key_length = (size_t)(equal - cursor);
            size_t value_length = line_length - key_length - 1u;
            size_t index;
            char value[257];
            for (index = 0u; index < sizeof(allowed) / sizeof(allowed[0]); ++index) {
                if (strlen(allowed[index]) == key_length &&
                    memcmp(cursor, allowed[index], key_length) == 0 &&
                    copy_wpa_text(equal + 1, value_length, value, sizeof(value), 1)) {
                    if (!first) {
                        buffer_append(buffer, ",");
                    }
                    first = 0;
                    buffer_string(buffer, allowed[index]);
                    buffer_append(buffer, ":");
                    buffer_string(buffer, value);
                    break;
                }
            }
        }
        if (end == NULL) {
            break;
        }
        cursor = end + 1;
    }
    buffer_append(buffer, "}");
}

static void append_wpa_scan_results(JsonBuffer *buffer, const char *response)
{
    const char *cursor = strchr(response, '\n');
    size_t count = 0u;
    buffer_append(buffer, "[");
    if (strncmp(response, "bssid /", 7u) != 0 || cursor == NULL) {
        buffer_append(buffer, "]");
        return;
    }
    ++cursor;
    while (*cursor != '\0' && count < MAX_WIFI_SCAN_RESULTS) {
        const char *end = strchr(cursor, '\n');
        const char *fields[5];
        size_t lengths[5];
        size_t line_length = end == NULL ? strlen(cursor) : (size_t)(end - cursor);
        const char *field = cursor;
        size_t index;
        int valid = 1;
        int frequency = 0;
        int signal = 0;
        char values[5][257];
        for (index = 0u; index < 4u; ++index) {
            const char *tab = memchr(field, '\t', line_length - (size_t)(field - cursor));
            if (tab == NULL) {
                valid = 0;
                break;
            }
            fields[index] = field;
            lengths[index] = (size_t)(tab - field);
            field = tab + 1;
        }
        fields[4] = field;
        lengths[4] = line_length - (size_t)(field - cursor);
        for (index = 0u; valid && index < 5u; ++index) {
            valid = copy_wpa_text(
                fields[index],
                lengths[index],
                values[index],
                sizeof(values[index]),
                index == 4u
            );
        }
        if (valid && parse_decimal(values[1], 1, 100000, &frequency) &&
            parse_decimal(values[2], -1000, 1000, &signal)) {
            if (count > 0u) {
                buffer_append(buffer, ",");
            }
            buffer_append(buffer, "{\"bssid\":");
            buffer_string(buffer, values[0]);
            buffer_format(
                buffer,
                ",\"frequency_mhz\":%d,\"signal_dbm\":%d,\"flags\":",
                frequency,
                signal
            );
            buffer_string(buffer, values[3]);
            buffer_append(buffer, ",\"ssid\":");
            buffer_string(buffer, values[4]);
            buffer_append(buffer, "}");
            ++count;
        }
        if (end == NULL) {
            break;
        }
        cursor = end + 1;
    }
    buffer_append(buffer, "]");
}

static void append_wpa_networks(JsonBuffer *buffer, const char *response)
{
    const char *cursor = strchr(response, '\n');
    size_t count = 0u;
    buffer_append(buffer, "[");
    if (strncmp(response, "network id /", 12u) != 0 || cursor == NULL) {
        buffer_append(buffer, "]");
        return;
    }
    ++cursor;
    while (*cursor != '\0' && count < MAX_WIFI_NETWORKS) {
        const char *end = strchr(cursor, '\n');
        const char *fields[4];
        size_t lengths[4];
        size_t line_length = end == NULL ? strlen(cursor) : (size_t)(end - cursor);
        const char *field = cursor;
        size_t index;
        int valid = 1;
        int network_id = -1;
        char values[4][257];
        for (index = 0u; index < 3u; ++index) {
            const char *tab = memchr(field, '\t', line_length - (size_t)(field - cursor));
            if (tab == NULL) {
                valid = 0;
                break;
            }
            fields[index] = field;
            lengths[index] = (size_t)(tab - field);
            field = tab + 1;
        }
        fields[3] = field;
        lengths[3] = line_length - (size_t)(field - cursor);
        for (index = 0u; valid && index < 4u; ++index) {
            valid = copy_wpa_text(
                fields[index],
                lengths[index],
                values[index],
                sizeof(values[index]),
                index > 1u
            );
        }
        if (valid && parse_decimal(values[0], 0, 4095, &network_id)) {
            if (count > 0u) {
                buffer_append(buffer, ",");
            }
            buffer_format(buffer, "{\"network_id\":%d,\"ssid\":", network_id);
            buffer_string(buffer, values[1]);
            buffer_append(buffer, ",\"bssid\":");
            buffer_string(buffer, values[2]);
            buffer_append(buffer, ",\"flags\":");
            buffer_string(buffer, values[3]);
            buffer_append(buffer, "}");
            ++count;
        }
        if (end == NULL) {
            break;
        }
        cursor = end + 1;
    }
    buffer_append(buffer, "]");
}

static void append_radio_power_values(
    JsonBuffer *buffer,
    const char *domain,
    int *first
)
{
    RadioPower power;
    if (!radio_power(domain, &power)) {
        return;
    }
    if (!*first) {
        buffer_append(buffer, ",");
    }
    *first = 0;
    buffer_append(buffer, "\"powered\":");
    buffer_append(buffer, power.unblocked ? "true" : "false");
    buffer_append(buffer, ",\"hard_blocked\":");
    buffer_append(buffer, power.hard_blocked ? "true" : "false");
    buffer_append(buffer, ",\"power_control\":");
    buffer_string(buffer, power.writable ? "writable" : "read-only");
}

static void append_bluetooth_fallback_values(JsonBuffer *buffer)
{
    RadioPower radio;
    int has_radio = radio_power("bluetooth", &radio);
    if (bluetooth_management_index_missing()) {
        buffer_append(buffer, ",\"powered\":false,\"power_state\":\"off\"");
    } else {
        buffer_append(buffer, ",\"powered\":null,\"power_state\":\"unknown\"");
    }
    if (!has_radio) {
        buffer_append(buffer, ",\"power_control\":\"unavailable\"");
        return;
    }
    buffer_append(buffer, ",\"hard_blocked\":");
    buffer_append(buffer, radio.hard_blocked ? "true" : "false");
    buffer_append(buffer, ",\"rfkill_unblocked\":");
    buffer_append(buffer, radio.unblocked ? "true" : "false");
    buffer_append(buffer, ",\"rfkill_soft_blocked\":");
    buffer_append(buffer, radio.unblocked ? "false" : "true");
    buffer_append(buffer, ",\"power_control\":");
    buffer_string(
        buffer,
        radio.writable
            ? (bluetooth_management_index_missing()
                ? "rfkill-reprobe"
                : "rfkill")
            : "read-only"
    );
}

static void append_wifi_values(JsonBuffer *buffer, const Device *device, int *first)
{
    char *response;
    if (strcmp(device->detail, "wifi") != 0) {
        return;
    }
    if (!*first) {
        buffer_append(buffer, ",");
    }
    *first = 0;
    if (!wpa_available(device->name)) {
        buffer_append(
            buffer,
            "\"wifi_control\":\"unavailable\","
            "\"wifi_control_reason\":\"wpa-supplicant-control-unavailable\""
        );
        return;
    }
    response = (char *)malloc(WPA_RESPONSE_CAPACITY);
    if (response == NULL ||
        !wpa_request(device->name, "STATUS", response, WPA_RESPONSE_CAPACITY) ||
        strncmp(response, "FAIL", 4u) == 0) {
        buffer_append(
            buffer,
            "\"wifi_control\":\"degraded\","
            "\"wifi_control_reason\":\"wpa-supplicant-control-request-failed\""
        );
        free(response);
        return;
    }
    buffer_append(buffer, "\"wifi_control\":\"available\",\"wifi_status\":");
    append_wpa_status(buffer, response);
    buffer_append(buffer, ",\"scan_results\":");
    if (wpa_request(device->name, "SCAN_RESULTS", response, WPA_RESPONSE_CAPACITY)) {
        append_wpa_scan_results(buffer, response);
    } else {
        buffer_append(buffer, "[]");
    }
    buffer_append(buffer, ",\"configured_networks\":");
    if (wpa_request(device->name, "LIST_NETWORKS", response, WPA_RESPONSE_CAPACITY)) {
        append_wpa_networks(buffer, response);
    } else {
        buffer_append(buffer, "[]");
    }
    free(response);
}

static void append_bluetooth_devices(JsonBuffer *buffer)
{
    size_t index;
    buffer_append(buffer, "[");
    for (index = 0u; index < bluetooth_found_count; ++index) {
        if (index > 0u) {
            buffer_append(buffer, ",");
        }
        buffer_append(buffer, "{\"address\":");
        buffer_string(buffer, bluetooth_found[index].address);
        buffer_append(buffer, ",\"name\":");
        buffer_string(
            buffer,
            bluetooth_found[index].name[0] != '\0'
                ? bluetooth_found[index].name
                : bluetooth_found[index].address
        );
        buffer_format(
            buffer,
            ",\"address_type\":%d,\"rssi\":%d,\"pairable\":false}",
            bluetooth_found[index].address_type,
            bluetooth_found[index].rssi
        );
    }
    buffer_append(buffer, "]");
}

static int append_state(JsonBuffer *buffer, const Device *device, int persisted)
{
    int first = 1;
    int64_t brightness;
    char path[PATH_MAX];
    buffer_format(
        buffer,
        "{\"schema\":\"%s\",\"revision\":%" PRIu64 ",\"provider\":\"%s\",\"state\":{\"id\":",
        MANAGER_SCHEMA,
        revision_number,
        COMPONENT_ID
    );
    append_device_id(buffer, device);
    buffer_append(buffer, ",\"domain\":");
    buffer_string(buffer, device->domain);
    buffer_append(buffer, ",\"available\":true,\"values\":{");
    switch (device->kind) {
    case DEVICE_POWER:
        append_text_value(buffer, device, "status", "status", &first);
        append_text_value(buffer, device, "type", "type", &first);
        append_integer_value(buffer, device, "capacity", "capacity_percent", 0, 100, 0, &first);
        append_integer_value(buffer, device, "online", "online", 0, 1, 1, &first);
        break;
    case DEVICE_THERMAL:
        append_text_value(buffer, device, "type", "type", &first);
        append_integer_value(
            buffer,
            device,
            "temp",
            "temperature_millicelsius",
            -273150,
            1000000,
            0,
            &first
        );
        break;
    case DEVICE_BACKLIGHT:
        if (join_path(path, sizeof(path), device_root(device), device->name, "brightness") &&
            read_i64_file(path, 0, device->maximum, &brightness)) {
            buffer_format(
                buffer,
                "\"brightness\":%" PRId64 ",\"brightness_percent\":%" PRId64
                ",\"max_brightness\":%" PRId64,
                brightness,
                (brightness * 100 + device->maximum / 2) / device->maximum,
                device->maximum
            );
            first = 0;
        }
        break;
    case DEVICE_INPUT:
        buffer_append(buffer, "\"kind\":\"input-event\"");
        first = 0;
        break;
    case DEVICE_NETWORK:
        buffer_append(buffer, "\"kind\":");
        buffer_string(buffer, device->detail);
        first = 0;
        append_text_value(buffer, device, "operstate", "operstate", &first);
        append_text_value(buffer, device, "address", "address", &first);
        append_integer_value(buffer, device, "carrier", "carrier", 0, 1, 1, &first);
        append_integer_value(buffer, device, "mtu", "mtu", 68, INT32_MAX, 0, &first);
        if (strcmp(device->detail, "wifi") == 0) {
            append_radio_power_values(buffer, "network", &first);
            append_wifi_values(buffer, device, &first);
        }
        break;
    case DEVICE_BLUETOOTH:
        {
            BluetoothInfo info;
            buffer_append(
                buffer,
                "\"kind\":\"controller\",\"pairing_available\":false,"
                "\"pairing_reason\":\"pairing-not-supported\""
            );
            first = 0;
            if (bluetooth_info(device->name, &info)) {
                buffer_append(buffer, ",\"address\":");
                buffer_string(buffer, info.address);
                buffer_append(buffer, ",\"adapter_name\":");
                buffer_string(buffer, info.name);
                buffer_append(buffer, ",\"powered\":");
                buffer_append(buffer, info.powered ? "true" : "false");
                buffer_append(buffer, ",\"discoverable\":");
                buffer_append(buffer, info.discoverable ? "true" : "false");
                buffer_append(
                    buffer,
                    ",\"hard_blocked\":false,\"power_control\":\"management\","
                    "\"management_control\":\"available\","
                    "\"discovery_control\":\"available\",\"discovered_devices\":"
                );
                append_bluetooth_devices(buffer);
            } else {
                append_text_value(buffer, device, "address", "address", &first);
                append_bluetooth_fallback_values(buffer);
                buffer_append(
                    buffer,
                    ",\"management_control\":\"unavailable\","
                    "\"management_reason\":"
                );
                buffer_string(
                    buffer,
                    bluetooth_management_index_missing()
                        ? "controller-not-registered"
                        : "linux-management-control-unavailable"
                );
                buffer_append(buffer, ",\"management_error\":");
                buffer_string(buffer, bluetooth_management_error);
                buffer_append(buffer, ",\"discovery_control\":\"unavailable\"");
            }
        }
        break;
    case DEVICE_RFKILL_NETWORK:
    case DEVICE_RFKILL_BLUETOOTH:
        buffer_append(buffer, "\"kind\":\"rfkill\"");
        first = 0;
        append_integer_value(buffer, device, "hard", "hard_blocked", 0, 1, 1, &first);
        if (join_path(path, sizeof(path), device_root(device), device->name, "soft") &&
            read_i64_file(path, 0, 1, &brightness)) {
            if (device->kind == DEVICE_RFKILL_BLUETOOTH) {
                buffer_append(buffer, ",\"powered\":null,\"rfkill_unblocked\":");
                buffer_append(buffer, brightness == 0 ? "true" : "false");
                buffer_append(buffer, ",\"rfkill_soft_blocked\":");
                buffer_append(buffer, brightness == 0 ? "false" : "true");
            } else {
                buffer_append(buffer, ",\"powered\":");
                buffer_append(buffer, brightness == 0 ? "true" : "false");
            }
        }
        break;
    }
    if (persisted >= 0) {
        if (!first) {
            buffer_append(buffer, ",");
        }
        buffer_append(buffer, "\"configuration_persisted\":");
        buffer_append(buffer, persisted ? "true" : "false");
    }
    buffer_append(buffer, "},\"mutable\":");
    append_mutable(buffer, device);
    buffer_append(buffer, "}}");
    return !buffer->failed;
}

static int parse_id_request(
    const char *json,
    const JsonToken *tokens,
    int count,
    const char *const *allowed,
    size_t allowed_count,
    char *identifier,
    size_t identifier_capacity
)
{
    int id;
    int refresh;
    int boolean_value;
    if (!object_validate_fields(json, tokens, count, 0, allowed, allowed_count)) {
        return 0;
    }
    id = object_field(json, tokens, count, 0, "id");
    if (id < 0 || !copy_string(json, &tokens[id], identifier, identifier_capacity) ||
        strchr(identifier, ':') == NULL) {
        return 0;
    }
    refresh = object_field(json, tokens, count, 0, "refresh");
    if (refresh == -2 ||
        (refresh >= 0 && !token_bool(json, &tokens[refresh], &boolean_value))) {
        return 0;
    }
    return 1;
}

static int build_get_state(
    const char *json,
    const JsonToken *tokens,
    int count,
    JsonBuffer *buffer
)
{
    static const char *const allowed[] = {"id", "refresh"};
    char identifier[24 + MAX_NAME + 2];
    DeviceList devices;
    const Device *device;
    if (!parse_id_request(
            json,
            tokens,
            count,
            allowed,
            2u,
            identifier,
            sizeof(identifier))) {
        return 0;
    }
    scan_devices(&devices);
    device = find_device(&devices, identifier);
    if (device == NULL) {
        return -1;
    }
    return append_state(buffer, device, -1) ? 1 : -2;
}

static int object_field_count(const JsonToken *tokens, int count, int object)
{
    int fields = 0;
    int cursor = object + 1;
    while (cursor < count && tokens[cursor].start < tokens[object].end) {
        int value = cursor + 1;
        if (tokens[cursor].parent != object || value >= count || tokens[value].parent != object) {
            return -1;
        }
        ++fields;
        cursor = token_next(tokens, count, value);
    }
    return fields;
}

static int configured_network_id(
    const char *interface,
    const char *ssid,
    int *network_id,
    int *matches
)
{
    char *response = (char *)malloc(WPA_RESPONSE_CAPACITY);
    const char *cursor;
    if (response == NULL ||
        !wpa_request(interface, "LIST_NETWORKS", response, WPA_RESPONSE_CAPACITY) ||
        strncmp(response, "network id /", 12u) != 0 ||
        (cursor = strchr(response, '\n')) == NULL) {
        free(response);
        return 0;
    }
    *network_id = -1;
    *matches = 0;
    ++cursor;
    while (*cursor != '\0' && *matches < 2) {
        const char *end = strchr(cursor, '\n');
        const char *first_tab;
        const char *second_tab;
        size_t line_length = end == NULL ? strlen(cursor) : (size_t)(end - cursor);
        first_tab = memchr(cursor, '\t', line_length);
        second_tab = first_tab == NULL
            ? NULL
            : memchr(
                first_tab + 1,
                '\t',
                line_length - (size_t)(first_tab + 1 - cursor)
            );
        if (first_tab != NULL && second_tab != NULL) {
            char id_text[16];
            char candidate[129];
            int parsed_id;
            if (copy_wpa_text(
                    cursor,
                    (size_t)(first_tab - cursor),
                    id_text,
                    sizeof(id_text),
                    0
                ) &&
                copy_wpa_text(
                    first_tab + 1,
                    (size_t)(second_tab - first_tab - 1),
                    candidate,
                    sizeof(candidate),
                    0
                ) &&
                parse_decimal(id_text, 0, 4095, &parsed_id) &&
                strcmp(candidate, ssid) == 0) {
                *network_id = parsed_id;
                ++*matches;
            }
        }
        if (end == NULL) {
            break;
        }
        cursor = end + 1;
    }
    free(response);
    return 1;
}

static int valid_psk(const char *psk)
{
    size_t length = strlen(psk);
    size_t index;
    if (length == 64u) {
        for (index = 0u; index < length; ++index) {
            if (!isxdigit((unsigned char)psk[index])) {
                return 0;
            }
        }
        return 1;
    }
    if (length < 8u || length > 63u) {
        return 0;
    }
    for (index = 0u; index < length; ++index) {
        unsigned char value = (unsigned char)psk[index];
        if (value < 0x20u || value > 0x7eu) {
            return 0;
        }
    }
    return 1;
}

static int psk_argument(const char *psk, char *output, size_t capacity)
{
    size_t length = strlen(psk);
    size_t read_position;
    size_t write_position = 0u;
    int hexadecimal = length == 64u;
    if (!valid_psk(psk)) {
        return 0;
    }
    if (hexadecimal) {
        if (length + 1u > capacity) {
            return 0;
        }
        for (read_position = 0u; read_position < length; ++read_position) {
            output[read_position] = (char)tolower((unsigned char)psk[read_position]);
        }
        output[length] = '\0';
        return 1;
    }
    if (capacity < 3u) {
        return 0;
    }
    output[write_position++] = '"';
    for (read_position = 0u; read_position < length; ++read_position) {
        char value = psk[read_position];
        if (value == '"' || value == '\\') {
            if (write_position + 2u >= capacity) {
                return 0;
            }
            output[write_position++] = '\\';
        } else if (write_position + 1u >= capacity) {
            return 0;
        }
        output[write_position++] = value;
    }
    if (write_position + 2u > capacity) {
        return 0;
    }
    output[write_position++] = '"';
    output[write_position] = '\0';
    return 1;
}

static void ssid_hex(const char *ssid, char output[65])
{
    static const char digits[] = "0123456789abcdef";
    const unsigned char *cursor = (const unsigned char *)ssid;
    size_t position = 0u;
    while (*cursor != '\0' && position < 64u) {
        unsigned char value = *cursor++;
        output[position++] = digits[(value >> 4u) & 0x0fu];
        output[position++] = digits[value & 0x0fu];
    }
    output[position] = '\0';
}

static int wifi_action(
    const char *json,
    const JsonToken *tokens,
    int count,
    int changes,
    const Device *device,
    int *persisted
)
{
    static const char *const allowed[] = {
        "action", "ssid", "psk", "network_id", "security"
    };
    int action_field;
    char action[32];
    int field_count = object_field_count(tokens, count, changes);
    if ((device->mutable & MUTABLE_ACTION) == 0 ||
        !object_validate_fields(json, tokens, count, changes, allowed, 5u) ||
        (action_field = object_field(json, tokens, count, changes, "action")) < 0 ||
        !copy_string(json, &tokens[action_field], action, sizeof(action))) {
        return 0;
    }
    *persisted = -1;
    if (strcmp(action, "scan") == 0 || strcmp(action, "disconnect") == 0) {
        if (field_count != 1) {
            return 0;
        }
        return wpa_ok(
            device->name,
            strcmp(action, "scan") == 0 ? "SCAN" : "DISCONNECT"
        ) ? 1 : -2;
    }
    if (strcmp(action, "forget") == 0) {
        int network_field = object_field(json, tokens, count, changes, "network_id");
        int64_t network_id;
        char command[64];
        if (field_count != 2 || network_field < 0 ||
            !token_i64(json, &tokens[network_field], &network_id) ||
            network_id < 0 || network_id > 4095) {
            return 0;
        }
        (void)snprintf(command, sizeof(command), "REMOVE_NETWORK %" PRId64, network_id);
        if (!wpa_ok(device->name, command)) {
            return -2;
        }
        *persisted = wpa_ok(device->name, "SAVE_CONFIG") ? 1 : 0;
        return 1;
    }
    if (strcmp(action, "connect") == 0) {
        int ssid_field = object_field(json, tokens, count, changes, "ssid");
        int psk_field = object_field(json, tokens, count, changes, "psk");
        int security_field = object_field(json, tokens, count, changes, "security");
        char ssid[129];
        char psk[65];
        char security[16];
        int network_id;
        int matches;
        int is_open = 0;
        char command[WPA_COMMAND_CAPACITY + 1u];
        if (ssid_field < 0 ||
            !copy_utf8_string(json, &tokens[ssid_field], ssid, sizeof(ssid), 32u) ||
            (psk_field >= 0 && security_field >= 0) || field_count < 2 || field_count > 3) {
            return 0;
        }
        if (security_field >= 0) {
            if (!copy_string(
                    json,
                    &tokens[security_field],
                    security,
                    sizeof(security)
                ) || strcmp(security, "open") != 0) {
                return 0;
            }
            is_open = 1;
        } else if (psk_field >= 0) {
            if (!copy_string(json, &tokens[psk_field], psk, sizeof(psk)) ||
                !valid_psk(psk)) {
                return 0;
            }
        }
        if (!configured_network_id(device->name, ssid, &network_id, &matches)) {
            return -2;
        }
        if (matches > 1) {
            return -2;
        }
        if (matches == 1) {
            if (psk_field >= 0 || security_field >= 0) {
                return 0;
            }
            (void)snprintf(command, sizeof(command), "ENABLE_NETWORK %d", network_id);
            if (!wpa_ok(device->name, command)) {
                return -2;
            }
            (void)snprintf(command, sizeof(command), "SELECT_NETWORK %d", network_id);
            *persisted = 1;
            return wpa_ok(device->name, command) ? 1 : -2;
        }
        if (psk_field < 0 && !is_open) {
            return 0;
        }
        {
            char response[32];
            if (!wpa_request(device->name, "ADD_NETWORK", response, sizeof(response)) ||
                !parse_decimal(response, 0, 4095, &network_id)) {
                return -2;
            }
        }
        {
            char encoded_ssid[65];
            ssid_hex(ssid, encoded_ssid);
            (void)snprintf(
                command,
                sizeof(command),
                "SET_NETWORK %d ssid %s",
                network_id,
                encoded_ssid
            );
            if (!wpa_ok(device->name, command)) {
                goto cleanup_network;
            }
        }
        if (is_open) {
            (void)snprintf(
                command,
                sizeof(command),
                "SET_NETWORK %d key_mgmt NONE",
                network_id
            );
        } else {
            char argument[132];
            if (!psk_argument(psk, argument, sizeof(argument))) {
                goto cleanup_network;
            }
            (void)snprintf(
                command,
                sizeof(command),
                "SET_NETWORK %d psk %s",
                network_id,
                argument
            );
        }
        if (!wpa_ok(device->name, command)) {
            goto cleanup_network;
        }
        (void)snprintf(command, sizeof(command), "ENABLE_NETWORK %d", network_id);
        if (!wpa_ok(device->name, command)) {
            goto cleanup_network;
        }
        (void)snprintf(command, sizeof(command), "SELECT_NETWORK %d", network_id);
        if (!wpa_ok(device->name, command)) {
            goto cleanup_network;
        }
        *persisted = wpa_ok(device->name, "SAVE_CONFIG") ? 1 : 0;
        return 1;

cleanup_network:
        (void)snprintf(command, sizeof(command), "REMOVE_NETWORK %d", network_id);
        (void)wpa_ok(device->name, command);
        return -2;
    }
    return 0;
}

static int bluetooth_action(
    const char *json,
    const JsonToken *tokens,
    int count,
    int changes,
    const Device *device
)
{
    static const char *const allowed[] = {"action"};
    int action_field;
    char action[32];
    if ((device->mutable & MUTABLE_ACTION) == 0 ||
        object_field_count(tokens, count, changes) != 1 ||
        !object_validate_fields(json, tokens, count, changes, allowed, 1u) ||
        (action_field = object_field(json, tokens, count, changes, "action")) < 0 ||
        !copy_string(json, &tokens[action_field], action, sizeof(action)) ||
        strcmp(action, "scan") != 0) {
        return 0;
    }
    return scan_bluetooth_devices(device->name) ? 1 : -2;
}

static int build_set_state(
    const char *json,
    const JsonToken *tokens,
    int count,
    JsonBuffer *buffer
)
{
    static const char *const allowed_root[] = {"id", "changes"};
    static const char *const allowed_backlight[] = {"brightness", "brightness_percent"};
    static const char *const allowed_rfkill[] = {"powered"};
    char identifier[24 + MAX_NAME + 2];
    int changes;
    int id;
    DeviceList devices;
    const Device *device;
    char path[PATH_MAX];
    int64_t requested;
    int persisted = -1;
    int field_count;
    if (!object_validate_fields(json, tokens, count, 0, allowed_root, 2u)) {
        return 0;
    }
    id = object_field(json, tokens, count, 0, "id");
    changes = object_field(json, tokens, count, 0, "changes");
    if (id < 0 || changes < 0 || tokens[changes].type != JT_OBJECT ||
        !copy_string(json, &tokens[id], identifier, sizeof(identifier))) {
        return 0;
    }
    field_count = object_field_count(tokens, count, changes);
    if (field_count <= 0) {
        return 0;
    }
    scan_devices(&devices);
    device = find_device(&devices, identifier);
    if (device == NULL) {
        return -1;
    }
    if (!device->mutable) {
        return -3;
    }
    if (device->kind == DEVICE_NETWORK &&
        object_field(json, tokens, count, changes, "action") >= 0) {
        int result = wifi_action(
            json,
            tokens,
            count,
            changes,
            device,
            &persisted
        );
        if (result <= 0) {
            return result;
        }
    } else if (device->kind == DEVICE_BLUETOOTH &&
               object_field(json, tokens, count, changes, "action") >= 0) {
        int result = bluetooth_action(json, tokens, count, changes, device);
        if (result <= 0) {
            return result;
        }
    } else if (field_count != 1) {
        return 0;
    } else if (device->kind == DEVICE_BACKLIGHT) {
        int brightness;
        int percent;
        if (!object_validate_fields(json, tokens, count, changes, allowed_backlight, 2u)) {
            return 0;
        }
        brightness = object_field(json, tokens, count, changes, "brightness");
        percent = object_field(json, tokens, count, changes, "brightness_percent");
        if ((brightness >= 0) == (percent >= 0)) {
            return 0;
        }
        if (brightness >= 0) {
            if (!token_i64(json, &tokens[brightness], &requested) ||
                requested < 0 || requested > device->maximum) {
                return 0;
            }
        } else {
            int64_t requested_percent;
            if (!token_i64(json, &tokens[percent], &requested_percent) ||
                requested_percent < 0 || requested_percent > 100) {
                return 0;
            }
            requested = (requested_percent * device->maximum + 50) / 100;
        }
        if (!join_path(path, sizeof(path), device_root(device), device->name, "brightness") ||
            !write_i64_verified(path, requested)) {
            return -2;
        }
    } else if (device->kind == DEVICE_NETWORK ||
               device->kind == DEVICE_BLUETOOTH) {
        int powered;
        int powered_value;
        RadioPower power;
        if ((device->mutable & MUTABLE_STATE) == 0 ||
            !object_validate_fields(json, tokens, count, changes, allowed_rfkill, 1u)) {
            return 0;
        }
        powered = object_field(json, tokens, count, changes, "powered");
        if (powered < 0 || !token_bool(json, &tokens[powered], &powered_value)) {
            return 0;
        }
        if (device->kind == DEVICE_BLUETOOTH) {
            if (!request_bluetooth_power(device->name, powered_value)) {
                return -1;
            }
        } else {
            if (!radio_power(device->domain, &power) ||
                power.hard_blocked || !power.writable) {
                return -3;
            }
            if (!set_radio_power(device->domain, powered_value)) {
                return -2;
            }
        }
    } else if (device->kind == DEVICE_RFKILL_NETWORK ||
               device->kind == DEVICE_RFKILL_BLUETOOTH) {
        int powered;
        int powered_value;
        int64_t hard;
        if (!object_validate_fields(json, tokens, count, changes, allowed_rfkill, 1u)) {
            return 0;
        }
        powered = object_field(json, tokens, count, changes, "powered");
        if (powered < 0 || !token_bool(json, &tokens[powered], &powered_value)) {
            return 0;
        }
        if (!join_path(path, sizeof(path), device_root(device), device->name, "hard") ||
            !read_i64_file(path, 0, 1, &hard) || hard != 0) {
            return -3;
        }
        if (!join_path(path, sizeof(path), device_root(device), device->name, "soft") ||
            !write_i64_verified(path, powered_value ? 0 : 1)) {
            return -2;
        }
    } else {
        return -3;
    }
    ++revision_number;
    scan_devices(&devices);
    device = find_device(&devices, identifier);
    return device != NULL && append_state(buffer, device, persisted) ? 1 : -2;
}

static int parse_optional_domain(
    const char *json,
    const JsonToken *tokens,
    int count,
    const char *const *allowed,
    size_t allowed_count,
    char *domain,
    size_t capacity
)
{
    int field;
    if (!object_validate_fields(json, tokens, count, 0, allowed, allowed_count)) {
        return 0;
    }
    field = object_field(json, tokens, count, 0, "domain");
    if (field < 0) {
        domain[0] = '\0';
        return 1;
    }
    return copy_string(json, &tokens[field], domain, capacity) && domain_index(domain) >= 0;
}

static void append_candidate(JsonBuffer *buffer, const char *domain, int detailed)
{
    buffer_append(buffer, "{\"component\":\"" COMPONENT_ID
        "\",\"name\":\"MSYS Native Linux HAL\",\"version\":\"" HAL_VERSION
        "\",\"priority\":200");
    if (detailed) {
        char capability[64];
        buffer_append(buffer, ",\"domains\":[");
        buffer_string(buffer, domain);
        buffer_append(buffer, "],\"capabilities\":[");
        (void)snprintf(capability, sizeof(capability), "%s.inventory", domain);
        buffer_string(buffer, capability);
        buffer_append(buffer, ",");
        (void)snprintf(capability, sizeof(capability), "%s.state.read", domain);
        buffer_string(buffer, capability);
        if (strcmp(domain, "backlight") == 0 ||
            strcmp(domain, "network") == 0 ||
            strcmp(domain, "bluetooth") == 0) {
            (void)snprintf(capability, sizeof(capability), "%s.state.write", domain);
            buffer_append(buffer, ",");
            buffer_string(buffer, capability);
        }
        if (strcmp(domain, "network") == 0) {
            buffer_append(
                buffer,
                ",\"network.wifi.scan\",\"network.wifi.connect\"," 
                "\"network.wifi.disconnect\",\"network.wifi.forget\""
            );
        } else if (strcmp(domain, "bluetooth") == 0) {
            buffer_append(
                buffer,
                ",\"bluetooth.radio.power\",\"bluetooth.discovery.scan\","
                "\"bluetooth.pairing.unavailable\""
            );
        }
        buffer_append(buffer, "],\"selected\":false,\"active\":true,"
            "\"health\":{\"status\":\"unknown\",\"reason\":\"not-checked\"}");
    }
    buffer_append(buffer, "}");
}

static void append_provider_row(JsonBuffer *buffer, const char *domain, int detailed)
{
    buffer_append(buffer, "{\"domain\":");
    buffer_string(buffer, domain);
    buffer_append(buffer, ",\"selection\":\"automatic\",\"preferred\":null,"
        "\"active\":\"" COMPONENT_ID "\",\"candidates\":[");
    append_candidate(buffer, domain, detailed);
    buffer_append(buffer, "]}");
}

static int build_list_providers(
    const char *json,
    const JsonToken *tokens,
    int count,
    JsonBuffer *buffer
)
{
    static const char *const allowed[] = {"domain", "refresh", "probe"};
    char requested[32];
    int index;
    int first = 1;
    int field;
    int boolean_value;
    if (!parse_optional_domain(json, tokens, count, allowed, 3u, requested, sizeof(requested))) {
        return 0;
    }
    field = object_field(json, tokens, count, 0, "refresh");
    if (field == -2 || (field >= 0 && !token_bool(json, &tokens[field], &boolean_value))) {
        return 0;
    }
    field = object_field(json, tokens, count, 0, "probe");
    if (field == -2 || (field >= 0 && !token_bool(json, &tokens[field], &boolean_value))) {
        return 0;
    }
    buffer_format(
        buffer,
        "{\"schema\":\"%s\",\"revision\":%" PRIu64 ",\"providers\":[",
        MANAGER_SCHEMA,
        revision_number
    );
    for (index = 0; index < DOMAIN_COUNT; ++index) {
        if (requested[0] != '\0' && strcmp(requested, DOMAINS[index]) != 0) {
            continue;
        }
        if (!first) {
            buffer_append(buffer, ",");
        }
        first = 0;
        append_provider_row(buffer, DOMAINS[index], requested[0] != '\0');
    }
    buffer_append(buffer, "]}");
    return !buffer->failed;
}

static int token_is_null(const char *json, const JsonToken *token)
{
    return token->type == JT_PRIMITIVE &&
           token->end - token->start == 4 &&
           memcmp(json + token->start, "null", 4u) == 0;
}

static int build_selection(
    const char *json,
    const JsonToken *tokens,
    int count,
    JsonBuffer *buffer,
    int reset
)
{
    static const char *const select_allowed[] = {
        "domain", "component", "expected_revision", "allow_unavailable"
    };
    static const char *const reset_allowed[] = {"domain", "expected_revision"};
    const char *const *allowed = reset ? reset_allowed : select_allowed;
    size_t allowed_count = reset ? 2u : 4u;
    char domain[32];
    int field;
    int boolean_value;
    int64_t integer_value;
    if (!parse_optional_domain(
            json,
            tokens,
            count,
            allowed,
            allowed_count,
            domain,
            sizeof(domain)) ||
        domain[0] == '\0') {
        return 0;
    }
    field = object_field(json, tokens, count, 0, "expected_revision");
    if (field == -2 || (field >= 0 &&
        (!token_i64(json, &tokens[field], &integer_value) || integer_value < 0))) {
        return 0;
    }
    if (!reset) {
        field = object_field(json, tokens, count, 0, "allow_unavailable");
        if (field == -2 ||
            (field >= 0 && !token_bool(json, &tokens[field], &boolean_value))) {
            return 0;
        }
        field = object_field(json, tokens, count, 0, "component");
        if (field == -2) {
            return 0;
        }
        if (field >= 0 && !token_is_null(json, &tokens[field])) {
            char component[192];
            if (!copy_string(json, &tokens[field], component, sizeof(component)) ||
                strcmp(component, COMPONENT_ID) != 0) {
                return -1;
            }
        }
    }
    buffer_format(
        buffer,
        "{\"schema\":\"%s\",\"revision\":%" PRIu64 ",\"providers\":[",
        MANAGER_SCHEMA,
        revision_number
    );
    append_provider_row(buffer, domain, 1);
    buffer_append(buffer, "]}");
    return !buffer->failed;
}

static int build_get_provider(
    const char *json,
    const JsonToken *tokens,
    int count,
    JsonBuffer *buffer
)
{
    static const char *const allowed[] = {"domain", "component", "refresh", "probe"};
    char domain[32];
    int component;
    int field;
    int boolean_value;
    if (!parse_optional_domain(json, tokens, count, allowed, 4u, domain, sizeof(domain)) ||
        domain[0] == '\0') {
        return 0;
    }
    component = object_field(json, tokens, count, 0, "component");
    if (component >= 0) {
        char selected[192];
        if (!copy_string(json, &tokens[component], selected, sizeof(selected)) ||
            strcmp(selected, COMPONENT_ID) != 0) {
            return -1;
        }
    } else if (component == -2) {
        return 0;
    }
    field = object_field(json, tokens, count, 0, "refresh");
    if (field == -2 || (field >= 0 && !token_bool(json, &tokens[field], &boolean_value))) {
        return 0;
    }
    field = object_field(json, tokens, count, 0, "probe");
    if (field == -2 || (field >= 0 && !token_bool(json, &tokens[field], &boolean_value))) {
        return 0;
    }
    buffer_format(
        buffer,
        "{\"schema\":\"%s\",\"revision\":%" PRIu64 ",\"provider\":",
        MANAGER_SCHEMA,
        revision_number
    );
    append_candidate(buffer, domain, 1);
    buffer_append(buffer, "}");
    return !buffer->failed;
}

static int build_watch(
    const char *json,
    const JsonToken *tokens,
    int count,
    JsonBuffer *buffer
)
{
    static const char *const allowed[] = {"after_revision", "timeout_ms", "domains"};
    if (!object_validate_fields(json, tokens, count, 0, allowed, 3u)) {
        return 0;
    }
    buffer_format(
        buffer,
        "{\"schema\":\"%s\",\"revision\":%" PRIu64 ",\"events\":[]}",
        MANAGER_SCHEMA,
        revision_number
    );
    return !buffer->failed;
}

static int build_describe(
    const char *json,
    const JsonToken *tokens,
    int count,
    JsonBuffer *buffer
)
{
    if (!object_validate_fields(json, tokens, count, 0, NULL, 0u)) {
        return 0;
    }
    buffer_append(
        buffer,
        "{\"schema\":\"" NATIVE_SCHEMA "\",\"provider\":{\"id\":\"" COMPONENT_ID
        "\",\"name\":\"MSYS Native Linux HAL\",\"version\":\"" HAL_VERSION
        "\"},\"domains\":[\"power\",\"thermal\",\"backlight\",\"display\","
        "\"display-output\",\"input\",\"network\",\"bluetooth\"],"
        "\"capabilities\":[\"power.state.read\",\"thermal.state.read\","
        "\"backlight.state.read\",\"backlight.state.write\",\"input.inventory\","
        "\"network.state.read\",\"network.rfkill.write\",\"network.wifi.scan\","
        "\"network.wifi.connect\",\"network.wifi.disconnect\",\"network.wifi.forget\","
        "\"bluetooth.state.read\",\"bluetooth.rfkill.write\",\"bluetooth.discovery.scan\","
        "\"bluetooth.pairing.unavailable\"]}"
    );
    return !buffer->failed;
}

typedef enum {
    DISPATCH_OK,
    DISPATCH_BAD_PAYLOAD,
    DISPATCH_UNAVAILABLE,
    DISPATCH_READ_ONLY,
    DISPATCH_INTERNAL,
    DISPATCH_UNSUPPORTED
} DispatchResult;

static DispatchResult dispatch(
    const char *method,
    const char *payload,
    size_t payload_length,
    JsonBuffer *response
)
{
    JsonToken tokens[MAX_TOKENS];
    int count;
    int result;
    size_t ignored_count = 0u;
    if (!parse_payload(payload, payload_length, tokens, &count)) {
        return DISPATCH_BAD_PAYLOAD;
    }
    if (strcmp(method, "describe") == 0) {
        result = build_describe(payload, tokens, count, response);
    } else if (strcmp(method, "inventory") == 0) {
        result = build_inventory(payload, tokens, count, response, &ignored_count);
    } else if (strcmp(method, "get_state") == 0) {
        result = build_get_state(payload, tokens, count, response);
    } else if (strcmp(method, "set_state") == 0) {
        result = build_set_state(payload, tokens, count, response);
    } else if (strcmp(method, "list_providers") == 0) {
        result = build_list_providers(payload, tokens, count, response);
    } else if (strcmp(method, "get_provider") == 0) {
        result = build_get_provider(payload, tokens, count, response);
    } else if (strcmp(method, "watch") == 0) {
        result = build_watch(payload, tokens, count, response);
    } else if (strcmp(method, "select_provider") == 0) {
        result = build_selection(payload, tokens, count, response, 0);
    } else if (strcmp(method, "reset_provider") == 0) {
        result = build_selection(payload, tokens, count, response, 1);
    } else {
        return DISPATCH_UNSUPPORTED;
    }
    if (result > 0 && !response->failed) {
        return DISPATCH_OK;
    }
    if (result == 0) {
        return DISPATCH_BAD_PAYLOAD;
    }
    if (result == -1) {
        return DISPATCH_UNAVAILABLE;
    }
    if (result == -3) {
        return DISPATCH_READ_ONLY;
    }
    return DISPATCH_INTERNAL;
}

static const char *dispatch_code(DispatchResult result)
{
    switch (result) {
    case DISPATCH_BAD_PAYLOAD: return "HAL_BAD_PAYLOAD";
    case DISPATCH_UNAVAILABLE: return "HAL_UNAVAILABLE";
    case DISPATCH_READ_ONLY: return "HAL_READ_ONLY";
    case DISPATCH_UNSUPPORTED: return "HAL_UNSUPPORTED";
    case DISPATCH_INTERNAL: return "HAL_INTERNAL_ERROR";
    case DISPATCH_OK: break;
    }
    return "HAL_INTERNAL_ERROR";
}

static const char *dispatch_message(DispatchResult result)
{
    switch (result) {
    case DISPATCH_BAD_PAYLOAD: return "request payload is invalid";
    case DISPATCH_UNAVAILABLE: return "HAL device is unavailable";
    case DISPATCH_READ_ONLY: return "HAL device is read-only";
    case DISPATCH_UNSUPPORTED: return "method is not supported by native HAL phase 1";
    case DISPATCH_INTERNAL: return "native HAL operation failed";
    case DISPATCH_OK: break;
    }
    return "native HAL operation failed";
}

static int process_call(msys_mipc_client *client, const char *packet)
{
    uint64_t request_id;
    char method[96];
    const char *payload;
    size_t payload_length;
    JsonBuffer response;
    DispatchResult dispatched;
    int result;
    if (msys_mipc_json_get_u64(packet, "id", &request_id) != MSYS_MIPC_OK ||
        msys_mipc_json_get_string(packet, "method", method, sizeof(method), NULL) != MSYS_MIPC_OK ||
        msys_mipc_json_get_raw(packet, "payload", &payload, &payload_length) != MSYS_MIPC_OK ||
        payload_length > MAX_REQUEST_JSON) {
        return msys_mipc_send_error(
            client,
            0,
            "HAL_BAD_PAYLOAD",
            "mIPC call envelope is invalid"
        );
    }
    buffer_init(&response);
    dispatched = dispatch(method, payload, payload_length, &response);
    if (dispatched == DISPATCH_OK) {
        result = msys_mipc_send_return_json(client, request_id, response.data);
        if (result == MSYS_MIPC_OK && strcmp(method, "set_state") == 0) {
            char *payload_copy = (char *)malloc(payload_length + 1u);
            if (payload_copy != NULL) {
                char identifier[24 + MAX_NAME + 2];
                if (payload_length <= MAX_REQUEST_JSON) {
                    char *separator;
                    JsonBuffer event;
                    memcpy(payload_copy, payload, payload_length);
                    payload_copy[payload_length] = '\0';
                    if (msys_mipc_json_get_string(
                            payload_copy,
                            "id",
                            identifier,
                            sizeof(identifier),
                            NULL) == MSYS_MIPC_OK &&
                        (separator = strchr(identifier, ':')) != NULL) {
                        *separator = '\0';
                        buffer_init(&event);
                        buffer_format(
                            &event,
                            "{\"revision\":%" PRIu64
                            ",\"kind\":\"state-changed\",\"domain\":",
                            revision_number
                        );
                        buffer_string(&event, identifier);
                        buffer_append(&event, ",\"id\":");
                        *separator = ':';
                        buffer_string(&event, identifier);
                        buffer_append(&event, ",\"provider\":\"" COMPONENT_ID "\"}");
                        if (!event.failed) {
                            (void)msys_mipc_send_event_json(
                                client,
                                "msys.hal.changed",
                                event.data
                            );
                        }
                        buffer_free(&event);
                    }
                }
                free(payload_copy);
            }
        }
    } else {
        result = msys_mipc_send_error(
            client,
            request_id,
            dispatch_code(dispatched),
            dispatch_message(dispatched)
        );
    }
    buffer_free(&response);
    return result;
}

static long rss_kib(void)
{
    FILE *stream = fopen("/proc/self/status", "r");
    char line[256];
    long value = 0;
    if (stream == NULL) {
        return 0;
    }
    while (fgets(line, sizeof(line), stream) != NULL) {
        if (sscanf(line, "VmRSS: %ld kB", &value) == 1) {
            break;
        }
    }
    (void)fclose(stream);
    return value;
}

static int self_check(void)
{
    JsonToken tokens[MAX_TOKENS];
    int count;
    JsonBuffer inventory;
    size_t devices = 0u;
    int bluetooth_management = 0;
    int wifi_control = 0;
    const char request[] = "{}";
    {
        const char *root = root_path("MSYS_HAL_BLUETOOTH_ROOT", "/sys/class/bluetooth");
        char names[MAX_ENTRIES][MAX_NAME + 1];
        size_t available = list_entries(root, "hci", names);
        BluetoothInfo info;
        if (available > 0u && bluetooth_info(names[0], &info)) {
            bluetooth_management = 1;
        }
    }
    {
        const char *root = root_path("MSYS_HAL_NETWORK_ROOT", "/sys/class/net");
        char names[MAX_ENTRIES][MAX_NAME + 1];
        size_t available = list_entries(root, NULL, names);
        size_t index;
        for (index = 0u; index < available; ++index) {
            char wireless[PATH_MAX];
            struct stat status;
            char response[32];
            if (join_path(wireless, sizeof(wireless), root, names[index], "wireless") &&
                stat(wireless, &status) == 0 && S_ISDIR(status.st_mode) &&
                wpa_request(names[index], "PING", response, sizeof(response)) &&
                strcmp(response, "PONG") == 0) {
                wifi_control = 1;
                break;
            }
        }
    }
    buffer_init(&inventory);
    count = parse_json(request, sizeof(request) - 1u, tokens, MAX_TOKENS);
    if (count <= 0 || !build_inventory(request, tokens, count, &inventory, &devices)) {
        buffer_free(&inventory);
        return 1;
    }
    printf(
        "{\"ok\":true,\"version\":\"%s\",\"devices\":%zu,"
        "\"wifi_control\":%s,\"bluetooth_management\":%s,"
        "\"bluetooth_management_error\":\"%s\","
        "\"rss_kib\":%ld}\n",
        HAL_VERSION,
        devices,
        wifi_control ? "true" : "false",
        bluetooth_management ? "true" : "false",
        bluetooth_management_error,
        rss_kib()
    );
    buffer_free(&inventory);
    return 0;
}

static int run_component(void)
{
    msys_mipc_client client;
    char *packet;
    int result;
    result = msys_mipc_client_from_env(&client);
    if (result != MSYS_MIPC_OK) {
        fprintf(stderr, "msys-hal-native: invalid MSYS_CONTROL_FD\n");
        return 1;
    }
    packet = (char *)malloc(MSYS_MIPC_RECV_CAPACITY);
    if (packet == NULL) {
        return 1;
    }
    result = msys_mipc_send_hello_from_env(&client);
    if (result == MSYS_MIPC_OK) {
        result = msys_mipc_recv_json(
            &client,
            packet,
            MSYS_MIPC_RECV_CAPACITY,
            5000,
            NULL
        );
    }
    if (result == MSYS_MIPC_OK) {
        result = msys_mipc_send_ready(&client);
    }
    if (result != MSYS_MIPC_OK) {
        free(packet);
        return 1;
    }
    for (;;) {
        char type[32];
        result = msys_mipc_recv_json(
            &client,
            packet,
            MSYS_MIPC_RECV_CAPACITY,
            -1,
            NULL
        );
        if (result == MSYS_MIPC_EOF) {
            break;
        }
        if (result != MSYS_MIPC_OK) {
            free(packet);
            return 1;
        }
        if (msys_mipc_json_get_string(
                packet,
                "type",
                type,
                sizeof(type),
                NULL) != MSYS_MIPC_OK) {
            continue;
        }
        if (strcmp(type, "shutdown") == 0) {
            break;
        }
        if (strcmp(type, "call") == 0 && process_call(&client, packet) != MSYS_MIPC_OK) {
            free(packet);
            return 1;
        }
    }
    free(packet);
    msys_mipc_client_close(&client);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--self-check") == 0) {
        return self_check();
    }
    if (argc != 1) {
        fprintf(stderr, "usage: msys-hal-native [--self-check]\n");
        return 2;
    }
    return run_component();
}
