import logging

import numpy as np
import pandas as pd
import vigra

from dvidutils import LabelMapper

from .favorites import compute_favorites, mark_favorites, extract_favorites
from ..logging_setup import PrefixedLogger
from ..util import Timer
from ..util.box import box_to_slicing, round_box, overwrite_subvol
from ..util.grid import boxes_from_grid
from ..dvid import fetch_volume_box, fetch_labelarray_voxels, fetch_mappings

logger = logging.getLogger(__name__)

HEMIBRAIN_TAB_STARTS_INVERTED = {
    # These are the Y-boundaries in the original coordiante system,
    # before rotation into the DVID coordiante system.
    #
    # Source email:
    #   From: "Saalfeld, Stephan" <saalfelds@janelia.hhmi.org>
    #   Date: December 18, 2017 at 20:07:13 EST
    #   Subject: Re: Hemi-brain aligned
    
    # NOTE: These inverted coordinates do not correspond to DVID coordinates.
    #       See HEMIBRAIN_TAB_BOUNDARIES, below.
    22: 0,
    23: 2067,
    24: 4684,
    25: 7574,
    26: 10386,
    27: 13223, # (last slice is black, sorry)
    28: 15938,
    29: 18532,
    30: 21198,
    31: 23827,
    32: 26507,
    33: 29176,
    34: 31772,
    35: 34427, # (tab does not exist, but here's where it would start)
}

# This corresponds to the X-coordinates of the DVID volume.
# The last entry marks the end of the volume.
HEMIBRAIN_TAB_BOUNDARIES = 34427 - np.fromiter(HEMIBRAIN_TAB_STARTS_INVERTED.values(), np.int32)[::-1]
HEMIBRAIN_WIDTH = HEMIBRAIN_TAB_BOUNDARIES[-1] - HEMIBRAIN_TAB_BOUNDARIES[0]

# (These are X coordinates)
assert HEMIBRAIN_TAB_BOUNDARIES.tolist() == [0, 2655, 5251, 7920, 10600, 13229, 15895, 18489, 21204, 24041, 26853, 29743, 32360, 34427]


def find_all_hotknife_edges_for_plane(server, uuid, instance, plane_center_coord_s0, tile_shape_s0, plane_spacing_s0, min_overlap_s0=1, *, scale=0):
    """
    Find all hotknife edges around the given X-plane, found in batches according to tile_shape_s0.
     
    See find_hotknife_edges() for more details.
    """
    plane_box_s0 = fetch_volume_box(server, uuid, instance)
    plane_bounds_s0 = plane_box_s0[:,:2]

    edge_tables = []
    tile_boxes = boxes_from_grid(plane_bounds_s0, tile_shape_s0, clipped=True)
    for tile_index, tile_bounds_s0 in enumerate(tile_boxes):
        tile_logger = PrefixedLogger(logger, f"Tile {tile_index:03d}/{len(tile_boxes):03d}: ")
        edge_table = find_hotknife_edges( server,
                                          uuid,
                                          instance,
                                          plane_center_coord_s0,
                                          tile_bounds_s0,
                                          plane_spacing_s0,
                                          min_overlap_s0,
                                          scale=scale,
                                          supervoxels=True, # Compute edges across supervoxels, and then filter out already-merged bodies afterwards
                                          logger=tile_logger )
        edge_tables.append(edge_table)

    edge_table = pd.concat(edge_tables, ignore_index=True)
    assert (edge_table.columns == ['left', 'right', 'xa', 'ya', 'za', 'xb', 'yb', 'zb', 'overlap']).all()
    edge_table.columns = ['id_a', 'id_b', 'xa', 'ya', 'za', 'xb', 'yb', 'zb', 'overlap']
    assert edge_table['id_a'].dtype == np.uint64
    assert edge_table['id_b'].dtype == np.uint64
    
    # "Complete" mappings not necessary for our purposes.
    mapping = fetch_mappings(server, uuid, instance, as_array=True)
    mapper = LabelMapper(mapping[:,0], mapping[:,1])
    
    edge_table['body_a'] = mapper.apply(edge_table['id_a'].values, True)
    edge_table['body_b'] = mapper.apply(edge_table['id_b'].values, True)

    edge_table.query('body_a != body_b', inplace=True)
    return edge_table


def find_hotknife_edges(server, uuid, instance, plane_center_coord_s0, plane_bounds_s0, plane_spacing_s0, min_overlap_s0=1, *, scale=0, supervoxels=True, logger=logger):
    """
    Download two X-tiles (left, right) spaced equidistantly from the given center-coord
    (presumably a hot-knife boundary) and bounded by the given plane_bounds.
    
    Then feed them to match_overlaps() to find potential edges across the center plane.
    
    Returns the DataFrame from match_overlaps, adjusting coordinates/overlaps for scale.
    
    Note:
        All args ending with '_s0' should be provided in scale 0 units.
        Internally, they will be adjusted (depending on the 'scale' arg),
        and the results will be returned in scale-0 units.
    
    Args:
        plane_center_coord_s0: (int)
            An X-coordinate to center the analysis around.
            For example, selected from HEMIBRAIN_TAB_BOUNDARIES

        plane_bounds_s0: (2D box)
            The YZ box to extract from each left/right plane.
        
        plane_spacing_s0: int
            How many pixels from the center plane the extracted tiles should be.
            Note: Internally, this will be adjusted to ensure that the extracted
            tiles do not overlap with the center plane, regardless of scale.
        
        min_overlap_s0: int
            Edges with small overlap will be filtered out, according to this setting.
        
        scale:
            Which image scale to perform the analysis with.
        
        supervoxels:
            Whether or not to perform the analysis on supervoxel labels (default) or body labels.
        
        Returns:
            DataFrame with columns: ['left', 'right', 'xa', 'ya', 'za', 'xb', 'yb', 'zb', 'overlap']
            where 'left' and 'right' are the supervoxel (or body) IDs for each edge,
            and ('xa', 'ya', 'za') is a sample coordinate from within the left object,
            and ('xb', 'yb', 'zb') is within the right object.
            (If scale>0 is used, there is a small chance that these coordinates
            will not lie within the object, but only in pathological cases.)
    """
    plane_bounds_s0 = np.asarray(plane_bounds_s0)
    assert plane_bounds_s0.shape == (2,2), f'plane_bounds_s0 should be a box, i.e. [(y0,x0), (y1,x1)], not {plane_bounds_s0}'
    
    plane_center_coord = plane_center_coord_s0 // (2**scale)
    left_plane_coord = (plane_center_coord_s0 - plane_spacing_s0) // (2**scale)
    right_plane_coord = (plane_center_coord_s0 + plane_spacing_s0) // (2**scale)
    
    plane_bounds = round_box(plane_bounds_s0, 2**scale, 'out') // (2**scale)
    min_overlap = min_overlap_s0 // ((2**scale)**2)
    
    if left_plane_coord == plane_center_coord:
        left_plane_coord -= 1
    if right_plane_coord == plane_center_coord:
        right_plane_coord += 1

    left_img = fetch_plane(server, uuid, instance, left_plane_coord, plane_bounds, axisname='x', scale=scale, supervoxels=supervoxels, logger=logger)
    right_img = fetch_plane(server, uuid, instance, right_plane_coord, plane_bounds, axisname='x', scale=scale, supervoxels=supervoxels, logger=logger)

    assert left_img.shape[2] == right_img.shape[2] == 1
    left_img = left_img[:,:,0]
    right_img = right_img[:,:,0]

    with Timer("Finding overlap edges", logger):
        edge_table = match_overlaps(left_img, right_img, min_overlap, crossover_filter='exclude-identities', match_filter='favorites', logger=logger)

    # Rename axes (we passed ZY image, not a YX image)
    edge_table.rename(inplace=True, columns={'ya': 'za',
                                             'xa': 'ya',
                                             'yb': 'zb',
                                             'xb': 'yb'})
    # Append X cols
    edge_table['xa'] = np.int32(left_plane_coord)
    edge_table['xb'] = np.int32(right_plane_coord)

    # translate    
    edge_table.loc[:, ['za', 'ya']] += plane_bounds[0]
    edge_table.loc[:, ['zb', 'yb']] += plane_bounds[0]

    # rescale
    edge_table.loc[:, ['za', 'ya', 'xa']] *= (2**scale)
    edge_table.loc[:, ['zb', 'yb', 'xb']] *= (2**scale)
    edge_table['overlap'] *= (2**scale)**2

    # Friendly ordering
    edge_table.sort_values(['overlap'], inplace=True, ascending=False)
    edge_table = edge_table[['left', 'right', 'xa', 'ya', 'za', 'xb', 'yb', 'zb', 'overlap']]

    return edge_table
    

def fetch_plane(server, uuid, instance, plane_coord, plane_bounds=None, axisname='z', tile_shape=(1024,1024), scale=0, supervoxels=False, logger=logger):
    axis = 'zyx'.index(axisname.lower())

    if plane_bounds is None:
        plane_box = fetch_volume_box(server, uuid, instance)
        plane_box //= (2**scale)
        plane_box[:, axis] = (plane_coord, plane_coord+1)
    else:
        plane_box = np.asarray(plane_bounds).tolist()
        plane_box[0].insert(axis, plane_coord)
        plane_box[1].insert(axis, plane_coord+1)
        plane_box = np.asarray(plane_box)

    plane_vol = np.zeros(plane_box[1] - plane_box[0], np.uint64)

    tile_shape = list(tile_shape)
    tile_shape.insert(axis, 1)

    with Timer(f"Fetching tiles for plane {axisname}={plane_coord}", logger):
        for tile_box in boxes_from_grid(plane_box, tile_shape, clipped=True):
            tile = fetch_labelarray_voxels(server, uuid, instance, tile_box, scale, supervoxels=supervoxels)
            overwrite_subvol(plane_vol, tile_box - plane_box[0], tile)
    return plane_vol


# def contingency_table(left_vol, right_vol):
#     """
#     Return a pd.DataFrame with columns 'left', 'right' and 'overlap_size',
#     indicating the count of overlapping pixels for each segment in 'from' with segments in 'to'.
#     
#     Note: Internally, copies both volumes multiple times.
#           This function seems to require an extra ~5x RAM relative to the inputs.
#     """
#     assert left_vol.dtype == right_vol.dtype
#     dtype = left_vol.dtype
#     vols_combined = np.empty((left_vol.size,2), dtype)
#     vols_combined[:,0]= left_vol.flat
#     vols_combined[:,1]= right_vol.flat
#     vols_combined = vols_combined.reshape(-1).view([('left', dtype), ('right', dtype)])
#     pairs, counts = np.unique(vols_combined, return_counts=True)
#     return pd.DataFrame({'left': pairs['left'], 'right': pairs['right'], 'overlap_size': counts})

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
    df = pd.DataFrame({"left": left_vol.flat, "right": right_vol.flat}, dtype=left_vol.dtype)
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
    

def region_coordinates(label_img, label_list):
    """
    Find a sample coordinate within label_img for each ID in label_list.
    For label ID, the RegionCenter (center of mass) of the object is returned,
    unless the object is concave and its center of mass lies outside the object bounds.
    In that case an arbitrary point within the object is returned.

    For a 3D image, returns array of shape (N,3), in Z-Y-X order.
    For a 2D image, returns array of shape (N,2) in Y-X order.
    """
    label_list = np.asarray(label_list)
    ndim = label_img.ndim
    label_img = vigra.taggedView(label_img, 'zyx'[3-ndim:])
    assert label_img.dtype == np.uint32
    # Little trick here: Vigra requires an intensity (float) image, but we don't compute any features that need it.
    # To save some RAM, just provide a fake view of the original image, since it happens to be the same bitwidth
    acc = vigra.analysis.extractRegionFeatures(label_img.view(np.float32), label_img, ['RegionCenter', 'Coord<Minimum>', 'Coord<Maximum>'])
    coords = acc['RegionCenter'][(label_list,)].astype(np.int32)
    
    # Sample the image at the region centers.
    # If a region center doesn't happen to fall on the object,
    # Choose an arbitrary valid coordiante.
    samples = label_img[tuple(coords.transpose())]
    for i in (samples != label_list).nonzero()[0]:
        label = label_list[i]
        box = np.array([  acc['Coord<Minimum>'][label],
                        1+acc['Coord<Maximum>'][label] ]).astype(np.int32)
        subvol = label_img[box_to_slicing(*box)]
        
        # Hopefully the middle scan-order coord is roughly in the middle of the object.
        label_coords = np.transpose((subvol == label).nonzero())
        middle_coord = label_coords[len(label_coords)//2]
        middle_coord += box[0]
        coords[i] = middle_coord

    return coords


def match_overlaps(left_img, right_img, min_overlap=1, crossover_filter='exclude-all', match_filter=None, logger=logger):
    """
    Given two 2D label images, overlay them and find pairs of overlapping labels ('edges').
    Overlaps are computed after computing the connected components on each image,
    so disjoint portions of each object are treated independently for the purposes of "min_overlap"
    and "favorites", as desribed below.
    
    (Label ID 0 is ignored.)
    
    Args:
        left_img, right_img:
            2D label image (np.uint64 or np.uint32)
        
        min_overlap:
            Specifies a minimum number of voxels for an overlapping region to count as an edge.
            Overlapping regions smaller than this will not be returned in the results.
        
        crossover_filter:
            When an object is present in both images (as determined by its label ID)
            and it overlaps with itself, it is considered a "crossover".
            Generally, one is not interested in crossover "identity" edges,
            and maybe isn't interested in other edges involving crossovers.
            This argument specifies how to include "crossovers" in the results.
            
            Choices:
                - None:
                    No filtering; include all crossovers, even identities).
                - "exclude-identities":
                    Don't include identity edges, but allow crossover
                    objects to participate in other edges).
                - "exclude-all":
                    Don't include any crossover objects in the results at all.
        
        match_filter:
            Specifies whether/how to require that each edge returned represents a "favorite"
            overlap one or both of the two objects in the edge, i.e. whether or not the object
            on left is the highest-overlapping object with the one on the right (or vice-versa).
            Choices:
                - None:
                    No filtering; include all edges, even non-favorites.
                - "favorites":
                    Require that either the left or right edge is the favorite of the other.
                - "mutual-favorites":
                    Require that both left/right object in each edge chose each other as their favorite.

    Returns:
        DataFrame in which each row lists an edge, with the following columns (not necessarily in this order):
            ['left', 'right', 'overlap', 'ya', 'xa', 'yb', 'xb, 'left_cc', 'right_cc']

        The left/right columns are label IDs, overlap is the size (in pixels) of the overlap region,
        xa,ya are sample coordinates guaranteed to fall within the the left object,
        and likewise xb,yb for the right object.
        The 'left_cc' and 'right_cc' columns are the IDs of the object after connected components,
        (used internally but perhaps useful for debugging or analysis).
        
        Note:
            There may be "duplicate" edges if two left/right bodies overlap in multiple places.
            But the left_cc,right_cc pairs will never have duplicates. 
    """
    assert left_img.ndim == right_img.ndim == 2
    assert left_img.shape == right_img.shape
    left_img = vigra.taggedView(left_img, 'yx')
    right_img = vigra.taggedView(right_img, 'yx')

    assert min_overlap > 0
    assert crossover_filter in (None, 'exclude-identities', 'exclude-all')
    assert match_filter in (None, 'favorites', 'mutual-favorites')

    # It's convenient if left and right CC images use disjoint label sets,
    # so shift the right CC values up.
    with Timer("Computing left/right CC", logger):
        left_cc_img, left_mapping = compute_cc(left_img, 1)
        right_cc_img, right_mapping = compute_cc(right_img, left_mapping.index.max()+1)

    with Timer("Computing contingency table", logger):
        cc_overlap_sizes = contingency_table(left_cc_img, right_cc_img)

    cc_overlap_sizes = pd.DataFrame(cc_overlap_sizes)
    cc_overlap_sizes.reset_index(inplace=True)
    cc_overlap_sizes.rename(inplace=True, columns={'left': 'left_cc',
                                                   'right': 'right_cc',
                                                   'voxel_count': 'overlap'})

    # Drop edges involving 0
    logger.info(f"Found {len(cc_overlap_sizes)} unfiltered edges")
    
    with Timer("Dropping zeros", logger):
        cc_overlap_sizes.query('left_cc != 0 and right_cc != 0', inplace=True)
    
    if min_overlap > 1:
        with Timer(f"Filtering for overlap > {min_overlap} from {len(cc_overlap_sizes)}", logger):
            cc_overlap_sizes.query('overlap > @min_overlap', inplace=True)

    logger.info(f"Found {len(cc_overlap_sizes)} non-zero edges")
    
    # Append columns showing the original pixel values for each overlap component
    cc_overlap_sizes['left'] = left_mapping.loc[cc_overlap_sizes['left_cc']].values
    cc_overlap_sizes['right'] = right_mapping.loc[cc_overlap_sizes['right_cc']].values
    
    # "Crossover" objects are objects that exist on both sides overlap with themselves.
    if crossover_filter == 'exclude-identities':
        with Timer("Dropping identity edges", logger):
            # Crossovers are permitted, but obviously we still exclude completely identical matches.
            cc_overlap_sizes.query('left != right', inplace=True)
        logger.info(f"Found {len(cc_overlap_sizes)} edges after identity filter")

    elif crossover_filter == 'exclude-all':
        with Timer("Dropping crossover edges", logger):
            # Crossovers not permitted.
            # We exclude matches in which EITHER side is a 'crossover' object.
            crossover_table = cc_overlap_sizes.query('left == right')
            crossover_components = set(crossover_table['left_cc']) | set(crossover_table['right_cc']) #@UnusedVariable
            cc_overlap_sizes.query('left_cc not in @crossover_components and right_cc not in @crossover_components', inplace=True)
        logger.info(f"Found {len(cc_overlap_sizes)} edges after crossover filter")

    if match_filter in ('favorites', 'mutual-favorites'):
        with Timer("Dropping non-favorites", logger):
            # Rename columns and invert score before calling compute_favorites()
            renames = {'left_cc': 'body_a', 'right_cc': 'body_b', 'overlap': 'score'}
            edge_table = cc_overlap_sizes.rename(columns=renames)
            edge_table['score'] *= -1 # Negate. (compute_favorites() looks for MIN values)
            
            component_favorites = compute_favorites(edge_table)
            favorite_flags_df = mark_favorites(edge_table, component_favorites)
            favorite_edges = extract_favorites(edge_table, favorite_flags_df, (match_filter == 'mutual-favorites'))
    
            # Undo the renames and fix the scores (overlaps).
            renames = {v:k for k,v in renames.items()}
            favorite_edges.rename(columns=renames, inplace=True)
            favorite_edges['overlap'] *= -1
            
            # Overwrite
            cc_overlap_sizes = favorite_edges

        logger.info(f"Found {len(cc_overlap_sizes)} edges favorites filter")

    with Timer("Generating coordinates", logger):
        left_coords = region_coordinates(left_cc_img, cc_overlap_sizes['left_cc'])
        right_coords = region_coordinates(right_cc_img, cc_overlap_sizes['right_cc'])

    # Using conventions 'a' for left, 'b' for right
    cc_overlap_sizes['xa'] = left_coords[:,1]
    cc_overlap_sizes['ya'] = left_coords[:,0]

    cc_overlap_sizes['xb'] = right_coords[:,1]
    cc_overlap_sizes['yb'] = right_coords[:,0]

    logger.info(f"Returning {len(cc_overlap_sizes)} edges")
    return cc_overlap_sizes