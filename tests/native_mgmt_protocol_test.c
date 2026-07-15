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

enum fake_controller_state {
    FAKE_CONTROLLER_MISSING,
    FAKE_CONTROLLER_OFF,
    FAKE_CONTROLLER_ON,
};

static enum fake_controller_state fake_controller;
static int fake_hard_blocked;
static int fake_rfkill_unblocked;
static int fake_rfkill_writes[4];
static size_t fake_rfkill_write_count;
static unsigned int fake_read_info_calls;
static unsigned int fake_registration_after_reads;
static unsigned int fake_wait_total_ms;

static int fake_read_info(const char *interface, BluetoothInfo *info)
{
    if (strcmp(interface, "hci0") != 0 || info == NULL) {
        return 0;
    }
    fake_read_info_calls++;
    if (fake_controller == FAKE_CONTROLLER_MISSING && fake_rfkill_unblocked &&
        fake_registration_after_reads != 0u &&
        fake_read_info_calls >= fake_registration_after_reads) {
        fake_controller = FAKE_CONTROLLER_OFF;
    }
    if (fake_controller == FAKE_CONTROLLER_MISSING) {
        (void)snprintf(
            bluetooth_management_error,
            sizeof(bluetooth_management_error),
            "%s",
            "index-list:0"
        );
        return 0;
    }
    (void)snprintf(
        bluetooth_management_error,
        sizeof(bluetooth_management_error),
        "%s",
        "none"
    );
    memset(info, 0, sizeof(*info));
    info->index = 0;
    info->powered = fake_controller == FAKE_CONTROLLER_ON;
    return 1;
}

static int fake_write_management(const char *interface, int powered)
{
    if (strcmp(interface, "hci0") != 0 ||
        fake_controller == FAKE_CONTROLLER_MISSING) {
        return 0;
    }
    fake_controller = powered ? FAKE_CONTROLLER_ON : FAKE_CONTROLLER_MISSING;
    return 1;
}

static int fake_read_rfkill(const char *domain, RadioPower *radio)
{
    if (strcmp(domain, "bluetooth") != 0 || radio == NULL) {
        return 0;
    }
    memset(radio, 0, sizeof(*radio));
    memcpy(radio->name, "rfkill0", sizeof("rfkill0"));
    radio->hard_blocked = fake_hard_blocked;
    radio->unblocked = fake_rfkill_unblocked;
    radio->writable = !fake_hard_blocked;
    return 1;
}

static int fake_write_rfkill(const char *domain, int unblocked)
{
    if (strcmp(domain, "bluetooth") != 0 || fake_hard_blocked ||
        fake_rfkill_write_count >=
            sizeof(fake_rfkill_writes) / sizeof(fake_rfkill_writes[0])) {
        return 0;
    }
    fake_rfkill_writes[fake_rfkill_write_count++] = unblocked;
    fake_rfkill_unblocked = unblocked;
    if (!unblocked) {
        fake_controller = FAKE_CONTROLLER_MISSING;
    }
    return 1;
}

static void fake_wait(unsigned int milliseconds)
{
    fake_wait_total_ms += milliseconds;
}

static void reset_fake(enum fake_controller_state controller, int unblocked)
{
    fake_controller = controller;
    fake_hard_blocked = 0;
    fake_rfkill_unblocked = unblocked;
    memset(fake_rfkill_writes, 0, sizeof(fake_rfkill_writes));
    fake_rfkill_write_count = 0u;
    fake_read_info_calls = 0u;
    fake_registration_after_reads = 2u;
    fake_wait_total_ms = 0u;
    (void)snprintf(
        bluetooth_management_error,
        sizeof(bluetooth_management_error),
        "%s",
        controller == FAKE_CONTROLLER_MISSING ? "index-list:0" : "none"
    );
}

int main(void)
{
    static const BluetoothPowerOps fake_operations = {
        fake_read_info,
        fake_write_management,
        fake_read_rfkill,
        fake_write_rfkill,
        fake_wait,
    };
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

    reset_fake(FAKE_CONTROLLER_ON, 1);
    fake_registration_after_reads = 0u;
    if (!request_bluetooth_power_with("hci0", 0, &fake_operations) ||
        fake_controller != FAKE_CONTROLLER_MISSING ||
        fake_rfkill_write_count != 0u) {
        return 8;
    }

    reset_fake(FAKE_CONTROLLER_MISSING, 1);
    if (!request_bluetooth_power_with("hci0", 1, &fake_operations) ||
        fake_controller != FAKE_CONTROLLER_ON ||
        fake_rfkill_write_count != 2u ||
        fake_rfkill_writes[0] != 0 || fake_rfkill_writes[1] != 1 ||
        fake_read_info_calls != 3u || fake_wait_total_ms < 200u) {
        return 9;
    }

    reset_fake(FAKE_CONTROLLER_MISSING, 0);
    if (!request_bluetooth_power_with("hci0", 1, &fake_operations) ||
        fake_controller != FAKE_CONTROLLER_ON ||
        fake_rfkill_write_count != 1u || fake_rfkill_writes[0] != 1 ||
        fake_read_info_calls != 3u) {
        return 10;
    }

    reset_fake(FAKE_CONTROLLER_MISSING, 1);
    if (!request_bluetooth_power_with("hci0", 0, &fake_operations) ||
        fake_rfkill_write_count != 0u) {
        return 11;
    }

    reset_fake(FAKE_CONTROLLER_MISSING, 1);
    fake_hard_blocked = 1;
    if (request_bluetooth_power_with("hci0", 1, &fake_operations) ||
        fake_rfkill_write_count != 0u) {
        return 12;
    }

    reset_fake(FAKE_CONTROLLER_MISSING, 1);
    fake_registration_after_reads = 5u;
    if (!request_bluetooth_power_with("hci0", 1, &fake_operations) ||
        fake_controller != FAKE_CONTROLLER_ON ||
        fake_read_info_calls != 6u ||
        fake_rfkill_write_count != 2u ||
        fake_rfkill_writes[0] != 0 || fake_rfkill_writes[1] != 1) {
        return 13;
    }

    reset_fake(FAKE_CONTROLLER_MISSING, 1);
    fake_registration_after_reads = 0u;
    if (request_bluetooth_power_with("hci0", 1, &fake_operations) ||
        fake_controller != FAKE_CONTROLLER_MISSING ||
        fake_read_info_calls != 51u ||
        fake_rfkill_write_count != 2u ||
        fake_rfkill_writes[0] != 0 || fake_rfkill_writes[1] != 1 ||
        fake_wait_total_ms != 5100u) {
        return 14;
    }

    (void)close(sockets[0]);
    (void)close(sockets[1]);
    return 0;
}
