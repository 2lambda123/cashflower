import argparse
import datetime
import functools
import getpass
import importlib
import inspect
import multiprocessing
import networkx as nx
import numpy as np
import os
import pandas as pd
import shutil

from .core import ArrayVariable, Model, ModelPointSet, Runplan, StochasticVariable, Variable
from .error import CashflowModelError
from .graph import create_directed_graph, filter_variables_and_graph, get_calls, get_predecessors, set_calc_direction
from .utils import get_git_commit_info, get_object_by_name, print_log, save_log_to_file


def create_model(model):
    """Create a folder structure for a model."""
    template_path = os.path.join(os.path.dirname(__file__), "model_tpl")
    shutil.copytree(template_path, model)


def load_settings(settings=None):
    """Add missing settings."""
    initial_settings = {
        "AGGREGATE": True,
        "GROUP_BY_COLUMN": None,
        "ID_COLUMN": "id",
        "MULTIPROCESSING": False,
        "NUM_STOCHASTIC_SCENARIOS": None,
        "OUTPUT_COLUMNS": [],
        "SAVE_DIAGNOSTIC": True,
        "SAVE_LOG": True,
        "SAVE_OUTPUT": True,
        "T_MAX_CALCULATION": 720,
        "T_MAX_OUTPUT": 720,
    }

    if settings is None:
        return initial_settings

    # Update with the user settings
    for key, value in settings.items():
        initial_settings[key] = value

    # Maximal output t can't exceed maximal calculation t
    if initial_settings["T_MAX_CALCULATION"] < initial_settings["T_MAX_OUTPUT"]:
        out = initial_settings["T_MAX_OUTPUT"]
        cal = initial_settings["T_MAX_CALCULATION"]
        msg = (f"T_MAX_OUTPUT ('{out}') exceeds T_MAX_CALCULATION ('{cal}'); "
               f"T_MAX_OUTPUT adjusted to match T_MAX_CALCULATION.")
        print_log(msg)
        initial_settings["T_MAX_OUTPUT"] = initial_settings["T_MAX_CALCULATION"]

    return initial_settings


def get_runplan(input_members, args):
    """Get runplan object from input.py script. Assign version if provided in the CLI command."""
    runplan = None
    for name, item in input_members:
        if isinstance(item, Runplan):
            runplan = item
            break

    if runplan is not None and args.version is not None:
        runplan.version = args.version

    return runplan


def get_model_point_sets(input_members, settings, args):
    """Get model point set objects from input.py script."""
    model_point_set_members = [m for m in input_members if isinstance(m[1], ModelPointSet)]

    main = None
    model_point_sets = []
    for name, model_point_set in model_point_set_members:
        model_point_set.name = name
        model_point_set.settings = settings
        model_point_set.initialize()
        model_point_sets.append(model_point_set)
        if name == "main":
            main = model_point_set

    if main is None:
        raise CashflowModelError("\nA model must have a model point set named 'main'.")

    if args.id is not None:
        chosen_id = str(args.id)
        main.data = main.data.loc[chosen_id]

    return model_point_sets


def get_variables(model_members, settings):
    """Get model variables from model.py script."""
    variable_members = [m for m in model_members if isinstance(m[1], Variable)]
    variables = []

    for name, variable in variable_members:
        # Set name
        if name == "t":
            msg = f"\nA variable can not be named '{name}' because it is a system variable. Please rename it."
            raise CashflowModelError(msg)
        variable.name = name

        # Initiate empty results
        variable.result = np.empty(settings["T_MAX_CALCULATION"]+1)
        if isinstance(variable, StochasticVariable):
            if settings["NUM_STOCHASTIC_SCENARIOS"] is None:
                msg = (f"\n\nThe model contains stochastic variable ('{name}')."
                       f"\nPlease set the number of stochastic scenarios ('NUM_STOCHASTIC_SCENARIOS' in 'settings.py').")
                raise CashflowModelError(msg)

            variable.result_stoch = np.empty((settings["NUM_STOCHASTIC_SCENARIOS"], settings["T_MAX_CALCULATION"]+1))

        variables.append(variable)
    return variables


def prepare_model_input(settings, args):
    """Get input for the cash flow model."""
    input_module = importlib.import_module("input")
    model_module = importlib.import_module("model")

    # input.py contains runplan and model point sets
    input_members = inspect.getmembers(input_module)
    runplan = get_runplan(input_members, args)
    model_point_sets = get_model_point_sets(input_members, settings, args)

    # model.py contains model variables
    model_members = inspect.getmembers(model_module)
    variables = get_variables(model_members, settings)

    return runplan, model_point_sets, variables


def resolve_calculation_order(variables, output_columns):
    """Determines a safe execution order for variables to avoid recursion errors."""
    # [1] Dictionary of called functions (key = variable; value = other variables called by it)
    calls = {}
    for variable in variables:
        calls[variable] = get_calls(variable, variables)

    # [2] Create directed graph for all variables
    dg = create_directed_graph(variables, calls)

    # [3] User has chosen output so remove unneeded variables
    if output_columns is not None:
        variables, dg = filter_variables_and_graph(output_columns, variables, dg)

    # [4] Set calculation order of variables ('calc_order')
    calc_order = 0
    while dg.nodes:
        nodes_without_predecessors = [n for n in dg.nodes if len(list(dg.predecessors(n))) == 0]

        # [4a] There are variables without any predecessors
        if len(nodes_without_predecessors) > 0:
            for node in nodes_without_predecessors:
                calc_order += 1
                node.calc_order = calc_order
            dg.remove_nodes_from(nodes_without_predecessors)

        # [4b] There is a cyclic relationship between variables
        else:
            cycles = list(nx.simple_cycles(dg))
            cycles_without_predecessors = [c for c in cycles if len(get_predecessors(c[0], dg)) == len(c)]

            for cycle in cycles_without_predecessors:
                # [4b_1] Ensure that there are no ArrayVariables in cycles
                for variable in cycle:
                    if isinstance(variable, ArrayVariable):
                        msg = (f"Variable '{variable.name}' is part of a cycle so it can't be modelled as an array variable."
                               f"\nCycle: {cycle}"
                               f"\nPlease remove 'array=True' from the decorator and recode the variable.")
                        raise CashflowModelError(msg)

                # [4b_2] Set the calculation order within the cycle
                calls_t = {}  # dictionary of called functions but only for the same time period ("t")
                for variable in cycle:
                    calls_t[variable] = get_calls(variable, cycle, argument_t_only=True)

                # Create directed graph for cycle variables
                dg_cycle = create_directed_graph(cycle, calls_t)

                # Set 'cycle_order'
                cycle_order = 0
                while dg_cycle.nodes:
                    cycle_nodes_without_predecessors = [cn for cn in dg_cycle.nodes if len(list(dg_cycle.predecessors(cn))) == 0]
                    if len(cycle_nodes_without_predecessors) > 0:
                        for node in cycle_nodes_without_predecessors:
                            cycle_order += 1
                            node.cycle_order = cycle_order
                        dg_cycle.remove_nodes_from(cycle_nodes_without_predecessors)
                    else:
                        cycle_variable_nodes = [node.name for node in dg_cycle.nodes]
                        msg = (f"Circular relationship without time step difference is not allowed. "
                               f"Please review variables: {cycle_variable_nodes}."
                               f"\nIf circular relationship without time step difference is necessary in your project, "
                               f"please raise it on: github.com/acturtle/cashflower")
                        raise CashflowModelError(msg)

                # [4b_3] All the variables from a cycle have the same 'calc_order' value
                calc_order += 1
                for node in cycle:
                    node.calc_order = calc_order
                    node.cycle = True
                dg.remove_nodes_from(cycle)

    # [5] Sort variables for calculation order
    variables = sorted(variables, key=lambda x: (x.calc_order, x.cycle_order, x.name))

    # [6] Set calculation direction of calculation ('calc_direction' attribute)
    variables = set_calc_direction(variables)

    return variables


def start_single_core(settings, args):
    """Create and run a cash flow model."""
    # Prepare model components
    print_log("Reading model components...", show_time=True)
    runplan, model_point_sets, variables = prepare_model_input(settings, args)
    output_columns = None if len(settings["OUTPUT_COLUMNS"]) == 0 else settings["OUTPUT_COLUMNS"]
    variables = resolve_calculation_order(variables, output_columns)

    # Log runplan version and number of model points
    if runplan is not None:
        print_log(f"Runplan version: {runplan.version}")
    main = get_object_by_name(model_point_sets, "main")
    print_log(f"Number of model points: {len(main)}")

    # Run model on single core
    model = Model(variables, model_point_sets, settings)
    output, runtime = model.run()
    return output, runtime


def start_multiprocessing(part, settings, args):
    """Run subset of the model points using multiprocessing."""
    cpu_count = multiprocessing.cpu_count()
    one_core = part == 0

    # Prepare model components
    print_log("Reading model components...", show_time=True, visible=one_core)
    runplan, model_point_sets, variables = prepare_model_input(settings, args)
    output_columns = None if len(settings["OUTPUT_COLUMNS"]) == 0 else settings["OUTPUT_COLUMNS"]
    variables = resolve_calculation_order(variables, output_columns)

    # Log runplan version and number of model points
    if runplan is not None:
        print_log(f"Runplan version: {runplan.version}", visible=one_core)
    main = get_object_by_name(model_point_sets, "main")
    print_log(f"Number of model points: {len(main)}", visible=one_core)
    print_log(f"Multiprocessing on {cpu_count} cores", visible=one_core)
    print_log(f"Calculation of ca. {len(main) // cpu_count} model points per core", visible=one_core)

    # Run model on multiple cores
    model = Model(variables, model_point_sets, settings)
    model_run = model.run(part)

    if model_run is None:
        part_output, part_runtime = None, None
    else:
        part_output, part_runtime = model_run
    return part_output, part_runtime


def merge_part_outputs(part_outputs, settings):
    """Merge outputs from multiprocessing and save to files."""
    # Nones are returned, when number of policies < number of cpus
    part_outputs = [po for po in part_outputs if po is not None]

    # Merge or concatenate outputs into one
    if settings["AGGREGATE"]:
        output = functools.reduce(lambda x, y: x.add(y, fill_value=0), part_outputs)
        if settings["GROUP_BY_COLUMN"] is not None:
            # group_by_column should not be added up
            output[settings["GROUP_BY_COLUMN"]] = part_outputs[0][settings["GROUP_BY_COLUMN"]]
    else:
        output = pd.concat(part_outputs)

    return output


def merge_part_diagnostic(part_diagnostic):
    # Nones are returned, when number of policies < number of cpus
    part_diagnostic = [item for item in part_diagnostic if item is not None]
    total_runtimes = sum([item["runtime"] for item in part_diagnostic])
    diagnostic = part_diagnostic[0]
    diagnostic["runtime"] = total_runtimes
    return diagnostic


def run(settings=None, path=None):
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    settings = load_settings(settings)
    output, diagnostic = None, None

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", "-i")
    parser.add_argument("--version", "-v")
    args = parser.parse_args()

    # Start log
    if path is not None:
        print_log(f"Model: '{os.path.basename(path)}'", show_time=True)
        print_log(f"Path: {path}")
    else:
        print_log("Model", show_time=True)
    print_log(f"Timestamp: {timestamp}")
    print_log(f"User: '{getpass.getuser()}'")
    commit = get_git_commit_info()
    if commit is not None:
        print_log(f"Git commit: {commit}")
    print_log("")

    has_arguments = any(arg_value is not None for arg_value in vars(args).values())
    if has_arguments:
        print_log("Arguments:")
        for arg_name, arg_value in vars(args).items():
            if arg_value is not None:
                print_log(f'- {arg_name}: {arg_value}')
        print_log("")

    print_log("Settings:")
    for key, value in settings.items():
        msg = f"- {key}: {value}"
        print_log(msg)
    print_log("")

    # Run on single core
    if not settings["MULTIPROCESSING"]:
        output, diagnostic = start_single_core(settings, args=args)

    # Run on multiple cores
    if settings["MULTIPROCESSING"]:
        p = functools.partial(start_multiprocessing, settings=settings, args=args)
        cpu_count = multiprocessing.cpu_count()
        with multiprocessing.Pool(cpu_count) as pool:
            parts = pool.map(p, range(cpu_count))

        # Merge model outputs
        part_outputs = [p[0] for p in parts]
        output = merge_part_outputs(part_outputs, settings)

        # Merge runtimes
        if settings["SAVE_DIAGNOSTIC"]:
            part_diagnostic = [p[1] for p in parts]
            diagnostic = merge_part_diagnostic(part_diagnostic)

    # Add time column
    values = [*range(settings["T_MAX_OUTPUT"]+1)] * int(output.shape[0] / (settings["T_MAX_OUTPUT"]+1))
    output.insert(0, "t", values)
    print_log("Finished!", show_time=True)
    print_log("")

    # Save to csv files
    if settings["SAVE_OUTPUT"] or settings["SAVE_DIAGNOSTIC"] or settings["SAVE_LOG"]:
        if not os.path.exists("output"):
            os.makedirs("output")

        if settings["SAVE_OUTPUT"]:
            filepath = f"output/{timestamp}_output.csv"
            print_log(f"Saving output file: {filepath}", show_time=True)
            output.to_csv(filepath, index=False)

        if settings["SAVE_DIAGNOSTIC"]:
            filepath = f"output/{timestamp}_diagnostic.csv"
            print_log(f"Saving diagnostic file: {filepath}", show_time=True)
            diagnostic.to_csv(filepath, index=False)

        if settings["SAVE_LOG"]:
            filepath = f"output/{timestamp}_log.txt"
            print_log(f"Saving log file: {filepath}", show_time=True)
            save_log_to_file(timestamp)

    print(f"{'-' * 72}\n")
    return output
