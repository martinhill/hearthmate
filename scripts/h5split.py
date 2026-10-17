"""Split all datasets of an hdf5 file into two hdf5 files at a specific index.
"""
import sys
from pathlib import Path
import h5py
import numpy as np

def split_hdf5_file(input_path, output_path1, output_path2, split_idx):
    with h5py.File(input_path, 'r') as f_in, \
         h5py.File(output_path1, 'w') as f_out1, \
         h5py.File(output_path2, 'w') as f_out2:

        def split_dataset(name, node):
            if isinstance(node, h5py.Dataset):
                # Split the data along axis 0
                data = node[()]
                f_out1.create_dataset(name, data=data[:split_idx])
                if name == "combustion_times":
                    # Adjust combustion times in 2nd file to start at 0
                    data[split_idx:] -= data[split_idx]
                f_out2.create_dataset(name, data=data[split_idx:])
            elif isinstance(node, h5py.Group):
                # Create corresponding groups
                f_out1.create_group(name)
                f_out2.create_group(name)

        # Recursively visit all objects in the file
        f_in.visititems(split_dataset)



if __name__ == "__main__":
    try:
        input_path = Path(sys.argv[1])
        output1_path = Path(input_path.parent, input_path.stem + "_part1.h5")
        output2_path = Path(input_path.parent, input_path.stem + "_part2.h5")
        split_idx = int(sys.argv[2])
    except Exception:
        print("Usage: h5split.py <input_file:str> <split_idx:int>")
        raise

    # debug print(f"split_hdf5_file({input_path}, {output1_path}, {output2_path}, {split_idx})")
    split_hdf5_file(input_path, output1_path, output2_path, split_idx)
