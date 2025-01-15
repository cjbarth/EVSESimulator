import math
import random
import threading
import time
from typing import List

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Input, Static


class EventBus:
    def __init__(self):
        self.subscribers = {}
        self.lock = threading.Lock()

    def publish(self, topic, message):
        with self.lock:
            for callback in self.subscribers.get(topic, []):
                callback(message)

    def subscribe(self, topic, callback):
        with self.lock:
            if topic not in self.subscribers:
                self.subscribers[topic] = []
            self.subscribers[topic].append(callback)

    def unsubscribe(self, topic, callback):
        with self.lock:
            if topic in self.subscribers:
                try:
                    self.subscribers[topic].remove(callback)
                except ValueError:
                    # Nothing to remove, move on
                    pass
                if not self.subscribers[topic]:
                    del self.subscribers[topic]


class Device:
    def __init__(
        self,
        name: str,
        min_amp_draw: int,
        system,
        max_amp_draw: int = None,
        safe_amp_draw: int = 0,
        is_ev: bool = False,
        always_draw_min: bool = True,
        weight: int = 1,
    ):
        self.name = name
        self.min_amp_draw: int = min_amp_draw
        self.safe_amp_draw: int = safe_amp_draw
        self.max_amp_draw: int = max_amp_draw or min_amp_draw
        self.weight: int = weight
        self.system: DeviceSimulatorApp = system
        self.is_ev: bool = is_ev
        self.always_draw_min = always_draw_min
        self.current_amp_draw: int = 0
        self.desired_amp_draw: int = 0
        self.is_on = False
        self.is_connected = True  # Indicates if the device is connected
        self.waited_cycles = 0
        self.last_heartbeat = 0
        self.lock = threading.Lock()
        self.carryover_adjustment = 0

        if (
            self.is_ev
            and self.safe_amp_draw < self.min_amp_draw
            and self.safe_amp_draw > 0
        ):
            raise ValueError(
                "Safe amperage can't be a non-zero value lower the the specified minimum."
            )

    def set_draw(self, data):
        with self.lock:
            if not self.is_on:
                self.current_amp_draw = 0  # Ensure no power is drawn if device is off
                self.desired_amp_draw = 0
                self.waited_cycles = 0
                return

            if self.is_ev:  # EV-specific behavior
                self.last_heartbeat = time.time()
                if self.always_draw_min and self.current_amp_draw == 0:
                    # We just turned on and we are told to always draw this no matter the power availability
                    self.current_amp_draw = self.min_amp_draw
                    self.desired_amp_draw = self.min_amp_draw
                    return

                if not data:
                    available_amps = 0
                    if self.current_amp_draw > self.safe_amp_draw:
                        self.current_amp_draw = self.safe_amp_draw
                        self.desired_amp_draw = self.safe_amp_draw
                    if self.current_amp_draw != self.safe_amp_draw:
                        available_amps = self.safe_amp_draw
                else:
                    available_amps = self.system.total_amps - data["amps"]

                if available_amps < 0:  # EVs shed load to accommodate
                    overdraw = abs(available_amps)
                    max_delay = 3
                    if self.waited_cycles > max_delay:
                        shed_amount = min(overdraw, self.current_amp_draw)
                        self.waited_cycles = 0
                    else:
                        weight_pct = self.weight / self.system.total_amps

                        # Shed my weighted portion of the percent the system is over
                        (_, weighted_shed_amps) = math.modf(overdraw * (1 - weight_pct))

                        shed_amount = weighted_shed_amps
                        self.system.interaction_log.append(
                            f"{self.name} shedding {shed_amount}"
                        )
                        self.system.update_interaction_log()
                        self.waited_cycles += 1

                    self.desired_amp_draw = max(
                        self.min_amp_draw if self.always_draw_min else 0,
                        self.current_amp_draw - overdraw,
                    )
                    self.current_amp_draw -= shed_amount
                    if self.current_amp_draw < self.min_amp_draw:
                        self.desired_amp_draw = (
                            self.min_amp_draw if self.always_draw_min else 0
                        )
                        self.current_amp_draw = (
                            self.min_amp_draw if self.always_draw_min else 0
                        )
                elif available_amps >= 0 and self.current_amp_draw < self.max_amp_draw:
                    previous_desired_amp_draw = self.desired_amp_draw
                    self.desired_amp_draw = min(
                        self.max_amp_draw,
                        self.current_amp_draw + available_amps,
                    )
                    if self.desired_amp_draw < self.min_amp_draw:
                        self.desired_amp_draw = self.min_amp_draw

                    # If available power is unstable (>10% variance), then keep waiting
                    if (
                        abs(previous_desired_amp_draw - self.desired_amp_draw)
                        / self.desired_amp_draw
                        > 0.1
                    ):
                        return

                    # Power is available, increase demand

                    # Try to increase demand based on priority
                    weight_pct = self.weight / self.system.total_amps
                    random_pct = random.randint(0, 0xFFFFFFFF) / 0xFFFFFFFF
                    if random_pct < weight_pct:
                        # We "rolled" a high enough value, we can increase draw.

                        if self.current_amp_draw == 0:  # Not drawing power yet
                            if available_amps >= self.min_amp_draw:
                                self.current_amp_draw = self.min_amp_draw
                        else:
                            weighted_increase_amps = (
                                self.desired_amp_draw - self.current_amp_draw
                            ) * weight_pct

                            (self.carryover_adjustment, increase) = math.modf(
                                weighted_increase_amps + self.carryover_adjustment
                            )

                            self.current_amp_draw += min(1, increase)
                            if self.current_amp_draw > self.max_amp_draw:
                                self.current_amp_draw = self.max_amp_draw

            else:  # Non-EV behavior
                if self.current_amp_draw == 0:
                    self.current_amp_draw = self.min_amp_draw
                    self.desired_amp_draw = self.min_amp_draw

    def run(self):
        check_in_period_sec = 3
        if self.is_ev:
            self.system.event_bus.subscribe("meter/data", self.set_draw)

        while True:
            if self.is_ev and self.is_on:
                if time.time() - check_in_period_sec > self.last_heartbeat:
                    # If it has been too long since we heard from the MQTT server, enter safe mode
                    self.set_draw(None)
            else:
                self.set_draw(None)

            time.sleep(0.1)


class DeviceSimulatorApp(App):
    def __init__(self, total_amps: int, **kwargs):
        super().__init__(**kwargs)
        self.devices: List[Device] = []
        self.total_amps: int = total_amps
        self.interaction_log = []
        self.event_bus = EventBus()

    def add_device(self, device: Device):
        self.devices.append(device)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            DataTable(id="device_table"),
            Static(id="power_info", expand=True),
            Static(id="interaction_log", expand=True),
        )
        yield Input(
            placeholder="Toggle device by typing here or clicking status.",
            id="user_input",
        )
        yield Footer()

    def on_mount(self):
        # Initialize device table
        table = self.query_one("#device_table", DataTable)
        table.add_column("Device", key="device")
        table.add_column("Status", key="status")
        table.add_column("Connected", key="connected")
        table.add_column("Current (A)", key="A")
        table.add_column("Min (A)", key="min_draw")
        table.add_column("Desired (A)", key="desired_draw")
        table.add_column("Max (A)", key="max_draw")
        table.add_column("Priority", key="priority")
        self.update_device_table()

        # Start individual threads for each device
        for device in self.devices:
            threading.Thread(target=device.run, daemon=True).start()

        # Start simulation thread for reporting
        threading.Thread(target=self.run_simulation, daemon=True).start()
        threading.Thread(target=self.meter_loop, daemon=True).start()

    def meter_loop(self):
        while True:
            meter_reading = self.poll_meter()
            meter_data = {
                "amps": meter_reading,
            }
            self.event_bus.publish("meter/data", meter_data)
            time.sleep(1)  # Assume that data is updated every second

    def update_device_table(self):
        table = self.query_one("#device_table", DataTable)

        for device in self.devices:
            status = "ON" if device.is_on else "OFF"
            connected_status = "Yes" if device.is_connected else "No"
            current_draw_str = f"{device.current_amp_draw}"
            min_draw_str = f"{device.min_amp_draw}"
            desired_draw_str = f"{device.desired_amp_draw}"
            max_draw_str = f"{device.max_amp_draw}"
            priority_str = f"{device.weight}" if device.weight is not None else "N/A"

            try:
                _ = table.get_row_index(device.name)
                # Update existing row
                table.update_cell(
                    row_key=device.name, column_key="A", value=current_draw_str
                )
                table.update_cell(
                    row_key=device.name, column_key="status", value=status
                )
                table.update_cell(
                    row_key=device.name, column_key="connected", value=connected_status
                )
                table.update_cell(
                    row_key=device.name, column_key="min_draw", value=min_draw_str
                )
                table.update_cell(
                    row_key=device.name,
                    column_key="desired_draw",
                    value=desired_draw_str,
                )
                table.update_cell(
                    row_key=device.name, column_key="max_draw", value=max_draw_str
                )
                table.update_cell(
                    row_key=device.name, column_key="priority", value=priority_str
                )
            except Exception as e:
                # Add a new row if the device is not already in the table
                table.add_row(
                    device.name,
                    status,
                    connected_status,
                    current_draw_str,
                    min_draw_str,
                    desired_draw_str,
                    max_draw_str,
                    priority_str,
                    key=device.name,
                )

    def update_monitor_reading(self):
        amps = self.poll_meter()
        meter_reading = self.query_one("#power_info", Static)
        meter_reading.update(
            f"Total amps available: {self.total_amps} A\n"
            f"Current meter reading: {amps} A"
        )

    def update_interaction_log(self):
        log_widget = self.query_one("#interaction_log", Static)
        log_widget.update("\n".join(self.interaction_log[-5:]))

    def run_simulation(self):
        while True:
            self.update_device_table()
            self.update_monitor_reading()
            time.sleep(0.1)

    def on_input_submitted(self, event):
        user_input = event.value.strip()
        if user_input:
            toggled_devices = [name.strip().upper() for name in user_input.split(",")]
            for device in self.devices:
                if device.name.upper() in toggled_devices:
                    device.is_on = not device.is_on
                    device.run()
                    state = "ON" if device.is_on else "OFF"
                    self.interaction_log.append(
                        f"{device.name} toggled {state} via input."
                    )
                    self.update_device_table()
                    self.update_interaction_log()

    def on_data_table_cell_selected(self, event):
        table = event.control  # Correctly get the DataTable instance
        row_key = event.cell_key.row_key.value
        column_key = event.cell_key.column_key.value

        # Ensure we handle clicks only on the "Status" column
        if column_key == "status":
            # Find the corresponding device
            for device in self.devices:
                if device.name == row_key:
                    # Toggle the device state
                    device.is_on = not device.is_on
                    state = "ON" if device.is_on else "OFF"
                    self.interaction_log.append(
                        f"{device.name} toggled {state} via click."
                    )
                    break

        # Ensure we handle clicks only on the "Device" column
        if column_key == "device":
            for device in self.devices:
                if device.name == row_key:
                    device.is_connected = not device.is_connected

                    # Update UI to reflect connection state
                    if not device.is_connected:
                        self.event_bus.unsubscribe("meter/data", device.set_draw)
                        self.interaction_log.append(f"{device.name} disconnected.")
                    else:
                        self.event_bus.subscribe("meter/data", device.set_draw)
                        self.interaction_log.append(f"{device.name} reconnected.")
                    break

        self.update_device_table()
        self.update_interaction_log()

    def poll_meter(self):
        return sum([device.current_amp_draw for device in self.devices])


def main():
    num_evs = int(input("Enter the number of EVs: "))
    total_amps = int(input("Enter the total amps available (A): "))
    app = DeviceSimulatorApp(total_amps=total_amps)

    # Assign random priorities and add EVs
    for i in range(num_evs):
        weight = random.randint(1, total_amps)  # Random priority between 1 and num_evs
        app.add_device(
            Device(
                name=f"EV{i+1}",
                min_amp_draw=6,
                safe_amp_draw=6,
                max_amp_draw=48,
                is_ev=True,
                always_draw_min=True,
                weight=weight,
                system=app,
            )
        )

    # Add non-EV devices
    app.add_device(
        Device(
            name="AC",
            min_amp_draw=random.randint(20, 50),
            safe_amp_draw=None,
            max_amp_draw=None,
            is_ev=False,
            weight=None,
            system=app,
        )
    )
    app.add_device(
        Device(
            name="WH",
            min_amp_draw=random.randint(20, 50),
            safe_amp_draw=None,
            max_amp_draw=None,
            is_ev=False,
            weight=None,
            system=app,
        )
    )
    app.add_device(
        Device(
            name="HT",
            min_amp_draw=random.randint(20, 50),
            safe_amp_draw=None,
            max_amp_draw=None,
            is_ev=False,
            weight=None,
            system=app,
        )
    )

    app.run()


if __name__ == "__main__":
    main()
