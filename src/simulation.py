import simpy
import random
import pandas as pd
import numpy as np
import yaml
from pathlib import Path
from time import sleep
import timeit
from gymnasium import spaces
import gymnasium as gym


class Environment(gym.env):
    def __init__(
        self,
        run_until: int,
        resources_cfg: dict,
        products_cfg: dict,
        raw_material_cfg: dict,
        schedule_interval: int,
        buffer_adjust_interval: int,
        production_mode: str = "pull",  # TODO: Push system
        set_constraint: int = None,
        sim_monitor_interval: int = None,
        monitor_interval: int = None,
        warmup: bool = False,
        actions: str = "discret",
        env_mode: str = "simulation",
        seed: int = None,
    ):
        super().__init__()
        random.seed(seed)
        self.env = simpy.Environment()

        # Parameters
        self.resources_config = resources_cfg
        self.products_config = products_cfg
        self.raw_material = raw_material_cfg
        self.warmup = warmup
        self.run_until = run_until
        self.sim_monitor_interval = sim_monitor_interval
        self.monitor_interval = monitor_interval
        self.actions_type = actions
        self.env_mode = env_mode
        self.schedule_interval = schedule_interval
        self.buffer_adjust_interval = buffer_adjust_interval
        self.constraint = set_constraint

        self._create_resources()
        self._create_process()

    def _create_orders(self):
        self.finished_goods = {}
        self.wip_product_monitor = {}
        self.demand = {}
        self.demand_orders = {}
        self.actual_demand = {}
        self.delivered_ontime = {}
        self.delivered_late = {}
        self.tardiness_products = {}
        self.tardiness_system = []
        for product in self.products_config:
            self.finished_goods[product] = simpy.Container(self.env)
            self.finished_goods[product].put(self.shipping_buffer[product])
            self.finished_goods_monitor[product] = []

            self.wip_product_monitor[product] = []
            self.inventory_product_monitor[product] = []
            self.demand[product] = simpy.FilterStore(self.env)
            self.demand_orders[product] = simpy.FilterStore(self.env)
            self.actual_demand[product] = simpy.Container(self.env)

            self.delivered_ontime[product] = simpy.FilterStore(self.env)
            self.delivered_late[product] = simpy.FilterStore(self.env)
            self.tardiness_products[product] = []

        self.finished_orders = simpy.FilterStore(self.env)
        self.to_release = simpy.Store(self.env)
        self.wip_finished = simpy.FilterStore(self.env)
        self.wip_id = 0
        self.wip_orders = {}
        self.wip_system_monitor = []
        self.inventory_system_monitor = []
        self.finished_goods_system_monitor = []

    def _create_resources(self) -> None:
        self.resources = {}
        self.machine_down = {}
        self.last_process = {}

        for resource in self.resources_config:
            resource_config = self.resources_config.get(resource)
            quantity = resource_config.get("quantity", 1)

            self.resources[resource] = simpy.Resource(self.env, quantity)
            self.machine_down[resource] = self.env.event()
            self.machine_down[resource].succeed()
            self.last_process[resource] = 0

    def _create_process(self):
        self.processes_config = {}
        self.processes_output = {}
        self.processes_input = {}

        for product in self.products_config:
            processes = self.products_config[product].get("processes")
            for process in processes:
                self.processes_config[process] = processes[process]
                next_process = [
                    process for x in processes.values() if process in x["deps"]
                ]
                self.processes_config[process]["next"] = next_process

                self.processes_output[process] = simpy.Store(self.env)
                self.processes_input[process] = simpy.Store(self.env)

                # Start production by process

                self.env.process(self._production_pull_system(process))

            raw_material = self.products_config[product].get("raw_material")
            self.processes_output[raw_material] = simpy.Store(self.env)

    def _production_pull_system(self, process):
        resource = self.processes_config[process]["resource"]

        next_process = False
        for product in self.products_config:
            if self.products_config[product]["final_process"] == process:
                next_process = "final"
            else:
                next_process = self.processes_config[process]["next"]

        process_deps = self.processes_config[process]["deps"]

        while True:
            yield self.machine_down[resource]

            order = yield self.processes_output[process_deps[0]].get()

            product = order["product"]

            setup_time = 0
            if self.last_process[resource] != process:
                setup_dist = self.resources_config[resource]["setup"].get(
                    "dist", "constant"
                )
                setup_params = self.resources_config[resource]["setup"].get(
                    "params", [0]
                )
                setup_time = self.generate_random_number(setup_dist, setup_params)
                if self.env.now >= self.warmup:
                    self.setups_cout[resource] += 1
                    self.setups_time[resource] += setup_time

            self.last_process[resource] = process

            with self.resources[resource].request() as req:
                yield req

                yield self.env.timeout(setup_time)

                process_time_dist = self.processes_config[process][
                    "processing_time"
                ].get("dist")
                process_time_params = self.processes_config[process][
                    "processing_time"
                ].get("params")

                order_quantity = order.get("quantity")

                start_time = self.env.now

                for part in range(order_quantity):
                    processing_time = self.generate_random_number(
                        process_time_dist, process_time_params
                    )

                    yield self.env.timeout(processing_time)

                    if resource == self.constraint:
                        self.constraint_buffer_level.remove(product)

                order["processes"][process] = self.env.now

                if next_process == "final":
                    order["finished"] = self.env.now
                    yield self.finished_orders.put(order)
                else:
                    yield self.processes_output[process].put(order)
                    # TODO: Push System - logic to put on next process input
                    # next_resource = self.processes_config[next_process[0]]["resource"]
                    # yield self.processes_input[next_resource].put(order)

                end_time = self.env.now
                if self.env.now > self.warmup:
                    self.utilization[resource] += round(end_time - start_time, 8)
