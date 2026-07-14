#define main msys_hal_embedded_main
#include "../native/src/native_hal.c"
#undef main

static int send_complete(
    int descriptor,
    uint16_t opcode,
    uint16_t index,
    const unsigned char *response,
    size_t response_length
)
{
    unsigned char packet[6u + 3u + 16u];
    size_t payload_length = 3u + response_length;
    if (response_length > 16u) {
        return 0;
    }
    write_le16(packet, MGMT_EV_CMD_COMPLETE);
    write_le16(packet + 2u, index);
    write_le16(packet + 4u, (uint16_t)payload_length);
    write_le16(packet + 6u, opcode);
    packet[8u] = 0u;
    if (response_length > 0u) {
        memcpy(packet + 9u, response, response_length);
    }
    return write(descriptor, packet, 6u + payload_length) ==
        (ssize_t)(6u + payload_length);
}

int main(void)
{
    int sockets[2];
    unsigned char settings[4] = {0x81u, 0x00u, 0x00u, 0x00u};
    unsigned char copied[4] = {0u, 0u, 0u, 0u};
    size_t response_length = 0u;
    int result;

    if (socketpair(AF_UNIX, SOCK_DGRAM, 0, sockets) != 0) {
        return 1;
    }

    if (!send_complete(
            sockets[0],
            MGMT_OP_SET_POWERED,
            0u,
            settings,
            sizeof(settings)
        )) {
        return 2;
    }
    result = receive_mgmt_event(
        sockets[1],
        100,
        MGMT_OP_SET_POWERED,
        0u,
        NULL,
        0u,
        &response_length
    );
    if (result != 1 || response_length != sizeof(settings)) {
        return 3;
    }

    response_length = 0u;
    if (!send_complete(
            sockets[0],
            MGMT_OP_SET_POWERED,
            0u,
            settings,
            sizeof(settings)
        )) {
        return 4;
    }
    result = receive_mgmt_event(
        sockets[1],
        100,
        MGMT_OP_SET_POWERED,
        0u,
        copied,
        sizeof(copied),
        &response_length
    );
    if (result != 1 || response_length != sizeof(settings) ||
        memcmp(copied, settings, sizeof(settings)) != 0) {
        return 5;
    }

    if (!send_complete(
            sockets[0],
            MGMT_OP_SET_POWERED,
            0u,
            settings,
            sizeof(settings)
        )) {
        return 6;
    }
    result = receive_mgmt_event(
        sockets[1],
        100,
        MGMT_OP_SET_POWERED,
        0u,
        copied,
        sizeof(copied) - 1u,
        &response_length
    );
    if (result != -1) {
        return 7;
    }

    (void)close(sockets[0]);
    (void)close(sockets[1]);
    return 0;
}
