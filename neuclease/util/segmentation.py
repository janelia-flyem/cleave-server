from collections import OrderedDict

import vigra
import numpy as np
import pandas as pd
import skimage.measure as skm

from dvidutils import LabelMapper

from . import Grid, boxes_from_grid, box_intersection, box_to_slicing

BLOCK_STATS_DTYPES = OrderedDict([ ('segment_id', np.uint64),
                                   ('z', np.int32),
                                   ('y', np.int32),
                                   ('x', np.int32),
                                   ('count', np.uint32) ])


def block_stats_for_volume(block_shape, volume, physical_box):
    """
    Get the count of voxels for each segment (excluding segment 0)
    in each block within the given volume, returned as a DataFrame.
    
    Returns a DataFrame with the following columns:
        ['segment_id', 'z', 'y', 'x', 'count']
        where z,y,z are the starting coordinates of each block.
    """
    block_grid = Grid(block_shape)
    
    block_dfs = []
    block_boxes = boxes_from_grid(physical_box, block_grid)
    for box in block_boxes:
        clipped_box = box_intersection(box, physical_box) - physical_box[0]
        block_vol = volume[box_to_slicing(*clipped_box)]
        counts = pd.Series(block_vol.reshape(-1)).value_counts(sort=False)
        segment_ids = counts.index.values
        counts = counts.values.astype(np.uint32)

        box = box.astype(np.int32)

        block_df = pd.DataFrame( { 'segment_id': segment_ids,
                                   'count': counts,
                                   'z': box[0][0],
                                   'y': box[0][1],
                                   'x': box[0][2] } )

        # Exclude segment 0 from output
        block_df = block_df[block_df['segment_id'] != 0]

        block_dfs.append(block_df)

    brick_df = pd.concat(block_dfs, ignore_index=True)
    brick_df = brick_df[['segment_id', 'z', 'y', 'x', 'count']]
    assert list(brick_df.columns) == list(BLOCK_STATS_DTYPES.keys())
    return brick_df


def contingency_table(left_vol, right_vol):
    """
    Overlay left_vol and right_vol and compute the table of
    overlapping label pairs, along with the size of each overlapping
    region.
    
    Args:
        left_vol, right_vol:
            np.ndarrays of equal shape
    
    Returns:
        pd.Series of sizes with a multi-level index (left,right)
    """
    assert left_vol.shape == right_vol.shape
    df = pd.DataFrame( {"left": left_vol.reshape(-1),
                        "right": right_vol.reshape(-1)},
                       dtype=left_vol.dtype )
    sizes = df.groupby(['left', 'right']).size()
    sizes.name = 'voxel_count'
    return sizes


def compute_cc(img, min_component=1):
    """
    Compute the connected components of the given label image,
    and return a pd.Series that maps from the CC label back to the original label.
    
    Pixels of value 0 are treated as background and not labeled.
    
    Args:
        img:
            ND label image, either np.uint8, np.uint32, or np.uint64
        
        min_component:
            Output components will be indexed starting with this value
            (but 0 is not affected)
        
    Returns:
        img_cc, cc_mapping, where:
            - img_cc is the connected components image (as np.uint32)
            - cc_mapping is pd.Series, indexed by CC labels, data is original labels.
    """
    assert min_component > 0
    if img.dtype in (np.uint8, np.uint32):
        img_cc = vigra.analysis.labelMultiArrayWithBackground(img)
    elif img.dtype == np.uint64:
        # Vigra's labelMultiArray() can't handle np.uint64,
        # so we must convert it to np.uint32 first.
        # We can't simply truncate the label values,
        # so we "consecutivize" them.
        img32 = np.zeros_like(img, dtype=np.uint32, order='C')
        _, _, _ = vigra.analysis.relabelConsecutive(img, out=img32)
        img_cc = vigra.analysis.labelMultiArrayWithBackground(img32)
    else:
        raise AssertionError(f"Invalid label dtype: {img.dtype}")    
    
    cc_mapping_df = pd.DataFrame( { 'orig': img.flat, 'cc': img_cc.flat } )
    cc_mapping_df.drop_duplicates(inplace=True)
    
    if min_component > 1:
        img_cc[img_cc != 0] += np.uint32(min_component-1)
        cc_mapping_df.loc[cc_mapping_df['cc'] != 0, 'cc'] += np.uint32(min_component-1)

    cc_mapping = cc_mapping_df.set_index('cc')['orig']
    return img_cc, cc_mapping
    

def split_disconnected_bodies(labels_orig):
    """
    Produces 3D volume split into connected components.

    This function identifies bodies that are the same label
    but are not connected.  It splits these bodies and
    produces a dict that maps these newly split bodies to
    the original body label.

    Special exception: Segments with label 0 are not relabeled.

    Args:
        labels_orig (numpy.array): 3D array of labels

    Returns:
        (labels_new, new_to_orig)

        labels_new:
            The partially relabeled array.
            Segments that were not split will keep their original IDs.
            Among split segments, the largest 'child' of a split segment retains the original ID.
            The smaller segments are assigned new labels in the range (N+1)..(N+1+S) where N is
            highest original label and S is the number of new segments after splitting.
        
        new_to_orig:
            A pseudo-minimal (but not quite minimal) mapping of labels
            (N+1)..(N+1+S) -> some subset of (1..N),
            which maps new segment IDs to the segments they came from.
            Segments that were not split at all are not mentioned in this mapping,
            for split segments, every mapping pair for the split is returned, including the k->k (identity) pair.
    """
    # Compute connected components and cast back to original dtype
    labels_cc = skm.label(labels_orig, background=0, connectivity=1)
    assert labels_cc.dtype == np.int64
    if labels_orig.dtype == np.uint64:
        labels_cc = labels_cc.view(np.uint64)
    else:
        labels_cc = labels_cc.astype(labels_orig.dtype, copy=False)

    # Find overlapping segments between orig and CC volumes
    overlap_table_df = contingency_table(labels_orig, labels_cc).reset_index()
    assert overlap_table_df.columns.tolist() == ['left', 'right', 'voxel_count']
    overlap_table_df.columns = ['orig', 'cc', 'voxels']
    overlap_table_df.sort_values('voxels', ascending=False, inplace=True)
    
    # If a label in 'orig' is duplicated, it has multiple components in labels_cc.
    # The largest component gets to keep the original ID;
    # the other components must take on new values.
    # (The new values must not conflict with any of the IDs in the original, so start at orig_max+1)
    new_cc_pos = overlap_table_df['orig'].duplicated()
    orig_max = overlap_table_df['orig'].max()
    new_cc_values = np.arange(orig_max+1, orig_max+1+new_cc_pos.sum(), dtype=labels_orig.dtype)

    overlap_table_df['final_cc'] = overlap_table_df['orig'].copy()
    overlap_table_df.loc[new_cc_pos, 'final_cc'] = new_cc_values
    
    # Relabel the CC volume to use the 'final_cc' labels
    mapper = LabelMapper(overlap_table_df['cc'].values, overlap_table_df['final_cc'].values)
    mapper.apply_inplace(labels_cc)

    # Generate the mapping that could (if desired) convert the new
    # volume into the original one, as described in the docstring above.
    emitted_mapping_rows = overlap_table_df['orig'].duplicated(keep=False)
    emitted_mapping_pairs = overlap_table_df.loc[emitted_mapping_rows, ['final_cc', 'orig']].values

    # Use tolist() to ensure plain Python int types
    # (This is required by some client code in Evaluate.py)
    new_to_orig = dict(emitted_mapping_pairs.tolist())
    
    return labels_cc, new_to_orig

