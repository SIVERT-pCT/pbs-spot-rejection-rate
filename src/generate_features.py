# Limits threads used by numpy and scipy for certain features.
# Must be done before importing the libraries.
# Only one of the two is required, depending on the system,
# but just using both to not worry about where this is executed.
# Source: https://stackoverflow.com/a/48665619/19299651
import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

import shutil
import subprocess
from uuid import uuid4
import argparse
import glob
from multiprocessing import Pool
import pandas as pd
import numpy as np
import json
from tqdm import tqdm

import model.features as f
from physics.rsp_image import MetaImageRSPImage


def extract_feature_dict_from_file(file, shift_x, shift_y):
    metadata: dict
    with open(file) as metafile:
        metadata = json.load(metafile)

    base_features = metadata["ground_truth"]

    params = metadata["parameters"]
    beam_spot_x = params["beam_spot_x"]
    beam_spot_sigma_x = params["beam_spot_sigma_x"]
    beam_spot_y = params["beam_spot_y"]
    beam_spot_sigma_y = params["beam_spot_sigma_y"]

    base_features["beam_spot_x"] = beam_spot_x
    base_features["beam_spot_y"] = beam_spot_y

    energy = params["beam_energy"]
    base_features["energy"] = energy
    ranges = pd.read_csv("../data/proton_energies.csv")
    ranges["diff"] = np.abs(ranges["energy"] - energy)
    base_features["water_range"] = ranges["range"][ranges["diff"].argmin()]

    pra = params["phantom_rotation_angle"]
    base_features["phantom_rotation_angle"] = pra

    if shift_x != 0 or shift_y != 0:
        beam_spot_x += shift_x
        beam_spot_y += shift_y
        base_features["beam_spot_x_shifted"] = beam_spot_x
        base_features["beam_spot_y_shifted"] = beam_spot_y

        # Simulate treatment for the shifted position in order to determine the actual planned spot.
        # We only need stopping statistics in the phantom, so 2e4 primaries are sufficient.
        image_path = "../data/simulation-environment_bd9e87a7.sif"
        primaries = 20000
        tempdir = os.path.join("../data/workdirs/", str(uuid4()))
        os.mkdir(tempdir)
        subprocess.run(["singularity", "run", "-B", f"{tempdir}:/output", image_path, "-dc", "-dp",
                        "--phantom", "head", "-p", str(primaries), "-e", str(energy), "-pra", str(pra),
                        "--beam-spot-x", str(beam_spot_x), "--beam-spot-y", str(beam_spot_y),
                        "--record-phantom-prod-stop"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        tempfiles = os.listdir(tempdir)
        for tempfile in tempfiles:
            if tempfile.endswith(".json"):
                ground_truth_shifted = f.extract_ground_truth_from_stopping(os.path.join(tempdir, tempfile))
                for k, v in ground_truth_shifted.items():
                    base_features[f"{k}_shifted"] = v
        shutil.rmtree(tempdir, ignore_errors=True)

    data_file = os.path.join(os.path.dirname(file), metadata["output"]["output_files"][0])
    df = pd.DataFrame(np.load(data_file))
    features = f.extract_features_pseudopixels(df, base_features)

    rsp_file = "../data/imageDump.mhd"
    rsp_image = MetaImageRSPImage(rsp_file, rotation_angle=pra)
    rsp = rsp_image.get_world_voxels()
    rsp_features = f.extract_features_rsp(rsp, beam_spot_x, beam_spot_sigma_x, beam_spot_y, beam_spot_sigma_y)
    features.update(rsp_features)
    return features


def filter_input_files(input_files, ref_df):
    filtered_files = []
    for file in input_files:
        metadata: dict
        with open(file) as metafile:
            metadata = json.load(metafile)

        params = metadata["parameters"]
        beam_spot_x_mask = ref_df["beam_spot_x"] == params["beam_spot_x"]
        beam_spot_y_mask = ref_df["beam_spot_y"] == params["beam_spot_y"]
        energy_mask = ref_df["energy"] == params["beam_energy"]
        pra_mask = ref_df["phantom_rotation_angle"] == params["phantom_rotation_angle"]

        if len(ref_df[beam_spot_x_mask & beam_spot_y_mask & energy_mask & pra_mask]) != 0:
            filtered_files.append(file)
    return filtered_files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk generation of features given JSON metadata files")
    parser.add_argument("-o", dest="output_file", metavar="output_file", type=str, help="File to save features to.")
    parser.add_argument("-s", "--shift", dest="shift", metavar="shift", type=int, default=0, required=False,
                        help="If not 0, spots will be shifted in positive and negative x and y directions in 1 mm"
                             "intervals up to the specified shift value. Default: 0")
    parser.add_argument("-r", "--shift-from", dest="shift_from", metavar="shift_from", type=int, default=1,
                        required=False, help="Spot shifting will start from this value on. Default: 1")
    parser.add_argument("--ref", dest="reference_file", metavar="reference_file", type=str, default="", required=False,
                        help="Path to a CSV file containing the original features for the spots to shift."
                             "Optional, filters file_pattern when given. Ignored if shift=0.")
    parser.add_argument("file_pattern", metavar="file_pattern", type=str, help="The JSON file pattern to glob.")
    args = parser.parse_args()

    shift = int(args.shift)
    shift_from = int(args.shift_from)

    files = glob.glob(args.file_pattern)
    ref_file = args.reference_file
    ref_spots = None
    if ref_file != "":
        ref_spots = pd.read_csv(ref_file)
        files = filter_input_files(files, ref_spots)

    with Pool(processes=8) as pool, tqdm(total=len(files) * max(1, (shift - shift_from + 1)*4)) as progress_bar:
        results = []
        for file in files:
            if shift == 0:
                results.append(pool.apply_async(extract_feature_dict_from_file, (file, 0, 0),
                                                callback=lambda _: progress_bar.update(1)))
            else:
                for shift_i in range(shift_from, shift + 1):
                    results.append(pool.apply_async(extract_feature_dict_from_file, (file, shift_i, 0),
                                                    callback=lambda _: progress_bar.update(1)))
                    results.append(pool.apply_async(extract_feature_dict_from_file, (file, -shift_i, 0),
                                                    callback=lambda _: progress_bar.update(1)))
                    results.append(pool.apply_async(extract_feature_dict_from_file, (file, 0, shift_i),
                                                    callback=lambda _: progress_bar.update(1)))
                    results.append(pool.apply_async(extract_feature_dict_from_file, (file, 0, -shift_i),
                                                    callback=lambda _: progress_bar.update(1)))

        [r.wait() for r in results]

        features_df = pd.DataFrame([r.get() for r in results])
        features_df.to_csv(args.output_file, index=False)