import marimo

__generated_with = "0.21.1"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _():
    import marimo as mo
    import h5py
    import altair as alt
    import polars as pl
    from pathlib import Path
    import numpy as np
    import matplotlib.pyplot as plt

    return Path, alt, h5py, mo, np, pl, plt


@app.cell(hide_code=True)
def _(Path, mo):
    file_browser = mo.ui.file_browser(
        initial_path=Path("/Volumes/burnie/"), multiple=False, filetypes=[".h5"]
    )
    mo.vstack([file_browser])
    return (file_browser,)


@app.cell(hide_code=True)
def _(file_browser, h5py, mo, np):
    burn_file = file_browser.path()
    h5_file = h5py.File(burn_file, "r")
    _keys = list(h5_file.keys())
    burn_length = h5_file["sequence_ids"].shape[0]
    combustion_times = h5_file["combustion_times"][:]
    tmp36_temps = h5_file["tmp36_temps"][:]
    vent_positions = h5_file["vent_positions"][:]
    image_frames = h5_file["mlx90640_frames"]

    max_temps = np.max(image_frames, axis=(1, 2))
    mean_temps = np.mean(image_frames, axis=(1, 2))

    zoom_slider = mo.ui.slider(
        start=0,
        stop=burn_length - 1,
        value=burn_length - 1,
        label="Zoom",
        full_width=True,
    )
    return (
        combustion_times,
        image_frames,
        max_temps,
        mean_temps,
        tmp36_temps,
        vent_positions,
        zoom_slider,
    )


@app.cell(hide_code=True)
def _(
    alt,
    combustion_times,
    max_temps,
    mean_temps,
    mo,
    pl,
    tmp36_temps,
    vent_positions,
    zoom_slider,
):
    frame_slider = mo.ui.slider(
        start=0, stop=zoom_slider.value, label="Frame", full_width=True
    )

    _df = pl.DataFrame(
        {
            "time": combustion_times[: zoom_slider.value],
            "pipe_temperature": tmp36_temps[: zoom_slider.value],
            "max_temp": max_temps[: zoom_slider.value],
            "mean_temp": mean_temps[: zoom_slider.value],
            "vent_position": vent_positions[: zoom_slider.value],
        }
    )

    _temp_chart = (
        alt.Chart(
            _df.unpivot(index="time", on=["pipe_temperature", "max_temp", "mean_temp"])
        )
        .mark_line()
        .encode(
            x=alt.X("time", title="Time (s)"),
            y=alt.Y("value", title="Temperature (°C)"),
            color=alt.Color(
                "variable",
                scale=alt.Scale(
                    domain=["pipe_temperature", "max_temp", "mean_temp"],
                    range=["tomato", "orange"],
                ),
            ),
            tooltip=["time", "value", "variable"],
        )
        .properties(title="Temperature over Time", width=900, height=250)
    )

    _vent_chart = (
        alt.Chart(_df)
        .mark_line(color="steelblue")
        .encode(
            x=alt.X("time", title="Time (s)"),
            y=alt.Y("vent_position", title="Vent Position"),
            tooltip=["time", "vent_position"],
        )
        .properties(title="Vent Position over Time", width=900, height=250)
    )

    mo.vstack(
        [
            mo.ui.altair_chart(_temp_chart),
            mo.ui.altair_chart(_vent_chart),
            zoom_slider,
        ]
    )
    return (frame_slider,)


@app.cell(hide_code=True)
def thermal_image_explorer(
    combustion_times,
    frame_slider,
    image_frames,
    max_temps,
    mo,
    np,
    plt,
    vent_positions,
    zoom_slider,
):
    min_temp = np.min(image_frames[: zoom_slider.value])
    max_temp = np.max(max_temps[: zoom_slider.value])

    plt.close("all")
    frame = np.fliplr(image_frames[frame_slider.value])
    fig, ax = plt.subplots()
    im = ax.imshow(frame, cmap="hot", aspect="auto", vmin=min_temp, vmax=max_temp)
    fig.colorbar(im, ax=ax, label="Temperature (°C)", location='bottom')
    ax.set_title(
        f"Thermal Image at {combustion_times[frame_slider.value]}s, vent {vent_positions[frame_slider.value]:.1f}% open"
    )
    plt.tight_layout()
    thermal_plot = mo.ui.matplotlib(plt.gca())

    fig_dist, ax_dist = plt.subplots()
    ax_dist.hist(
        frame.flatten(), bins=50, color="steelblue", edgecolor="black", alpha=0.7
    )
    ax_dist.set_xlabel("Temperature (°C)")
    ax_dist.set_ylabel("Frequency")
    ax_dist.set_title("Temperature Distribution")
    plt.tight_layout()
    dist_plot = mo.ui.matplotlib(ax_dist)

    mo.vstack([mo.hstack([thermal_plot, dist_plot]), frame_slider])
    return


if __name__ == "__main__":
    app.run()
