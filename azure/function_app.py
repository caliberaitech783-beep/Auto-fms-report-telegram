"""Dedicated timer worker; no routes or changes to existing Azure applications."""
import azure.functions as func
from cloud_worker import run

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer",
                   run_on_startup=False, use_monitor=True)
def fleet_reports(timer: func.TimerRequest):
    run()
