from typing import List
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Header, Footer, Input, Static, DataTable
import threading
import random
import time


class Device:
    def __init__(self, name, min_draw, max_draw, is_ev, priority, system):
        self.name = name
        self.min_draw = min_draw
        self.max_draw = max_draw
        self.priority = priority
        self.system = system
        self.is_ev = is_ev
        self.current_draw = 0
        self.desired_draw = 0
        self.is_on = False
        self.wait_cycles = 0
        self.lock = threading.Lock()

    def set_draw(self):
        with self.lock:
            if not self.is_on:
                self.current_draw = 0  # Ensure no power is drawn if device is off
                self.desired_draw = 0
                self.wait_cycles = 0
                return

            if self.is_ev:  # EV-specific behavior
                available_power = self.system.total_power - self.system.poll_meter()
                if self.current_draw == 0:  # Not drawing power yet
                    if available_power >= self.min_draw:
                        self.desired_draw = min(self.max_draw, available_power)
                        self.wait_cycles += 1
                        if self.wait_cycles >= max(
                            self.min_draw, self.priority
                        ):  # Wait enough cycles for power to be consistently available
                            self.current_draw = self.min_draw
                            self.wait_cycles = 0
                    else:
                        self.desired_draw = 0
                        self.wait_cycles = (
                            0  # Reset wait cycles if power is insufficient
                        )
                else:  # Already drawing power
                    if available_power < 0:  # EVs shed load to accommodate
                        # Account for priority before shedding load
                        alpha = 1
                        beta = (self.priority / 2) ** 2 if self.priority else 1
                        # delay_value = random.betavariate(alpha, beta)
                        max_delay = 3
                        delay_value = (beta / (beta + alpha)) * (
                            1 / (1 + (self.priority or 1))
                        )
                        time.sleep(delay_value * max_delay)

                        available_power = (
                            self.system.total_power - self.system.poll_meter()
                        )
                        overdraw = abs(available_power)
                        shed_amount = min(overdraw, self.current_draw)
                        self.current_draw -= shed_amount
                        if self.current_draw < self.min_draw:
                            self.current_draw = (
                                0  # If shedding drops below minimum, draw 0
                            )
                    else:
                        available_power = (
                            self.system.total_power - self.system.poll_meter()
                        )
                        self.desired_draw = min(
                            self.max_draw,
                            self.current_draw + available_power,
                        )
                        increase = min(1, available_power)
                        if (
                            increase > 0
                            and self.wait_cycles >= self.priority
                            and self.current_draw < self.max_draw
                        ):
                            self.current_draw += increase
                            self.wait_cycles = 0
                        else:
                            self.wait_cycles += 1

            else:  # Non-EV behavior
                if self.current_draw == 0:
                    self.current_draw = self.min_draw
                    self.desired_draw = self.min_draw

    def run(self):
        while True:
            self.set_draw()
            if self.is_ev:
                time.sleep(random.uniform(0.8, 1.2))
            else:
                time.sleep(0.1)  # Non-EVs turn on an off quickly


class DeviceSimulatorApp(App):
    def __init__(self, total_power, **kwargs):
        super().__init__(**kwargs)
        self.devices = []
        self.total_power = total_power
        self.interaction_log = []

    def add_device(self, device):
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
        table.add_column("Device")
        table.add_column("Status", key="status")
        table.add_column("Current Power (kW)", key="kW")
        table.add_column("Min Draw (kW)", key="min_draw")
        table.add_column("Desired Draw (kW)", key="desired_draw")
        table.add_column("Max Draw (kW)", key="max_draw")
        table.add_column("Priority", key="priority")
        self.update_device_table()

        # Start individual threads for each device
        for device in self.devices:
            threading.Thread(target=device.run, daemon=True).start()

        # Start simulation thread for reporting
        threading.Thread(target=self.run_simulation, daemon=True).start()

    def update_device_table(self):
        table = self.query_one("#device_table", DataTable)

        for device in self.devices:
            status = "ON" if device.is_on else "OFF"
            current_draw_str = f"{device.current_draw} kW"
            min_draw_str = f"{device.min_draw} kW"
            desired_draw_str = f"{device.desired_draw} kW"
            max_draw_str = f"{device.max_draw} kW"
            priority_str = (
                f"{device.priority}" if device.priority is not None else "N/A"
            )

            try:
                _ = table.get_row_index(device.name)
                # Update existing row
                table.update_cell(
                    row_key=device.name, column_key="kW", value=current_draw_str
                )
                table.update_cell(
                    row_key=device.name, column_key="status", value=status
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
                    current_draw_str,
                    min_draw_str,
                    desired_draw_str,
                    max_draw_str,
                    priority_str,
                    key=device.name,
                )

    def update_power_info(self):
        total_draw = self.poll_meter()
        power_info = self.query_one("#power_info", Static)
        power_info.update(
            f"Total Power Available: {self.total_power} kW\n"
            f"Total Power Draw: {total_draw} kW"
        )

    def update_interaction_log(self):
        log_widget = self.query_one("#interaction_log", Static)
        log_widget.update("\n".join(self.interaction_log[-5:]))

    def run_simulation(self):
        while True:
            self.update_device_table()
            self.update_power_info()
            time.sleep(0.2)

    def on_input_submitted(self, event):
        user_input = event.value.strip()
        if user_input:
            toggled_devices = [name.strip().upper() for name in user_input.split(",")]
            for device in self.devices:
                if device.name.upper() in toggled_devices:
                    device.is_on = not device.is_on
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
                    self.update_device_table()
                    self.update_interaction_log()
                    break

    def poll_meter(self):
        return sum([device.current_draw for device in self.devices])


def main():
    num_evs = int(input("Enter the number of EVs: "))
    total_power = int(input("Enter the total power available (kW): "))
    app = DeviceSimulatorApp(total_power)

    # Assign random priorities and add EVs
    for i in range(num_evs):
        priority = random.randint(1, num_evs)  # Random priority between 1 and num_evs
        app.add_device(Device(f"EV{i+1}", 2, 12, True, priority, app))

    # Add non-EV devices
    app.add_device(Device("AC", random.randint(2, 12), None, False, None, app))
    app.add_device(Device("WH", random.randint(2, 12), None, False, None, app))
    app.add_device(Device("HT", random.randint(2, 12), None, False, None, app))

    app.run()


if __name__ == "__main__":
    main()
