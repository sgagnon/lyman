import os
import os.path as op

import numpy as np
import pandas as pd
import nibabel as nib
from scipy.ndimage import binary_dilation
from skimage import morphology
import matplotlib as mpl
import matplotlib.pyplot as plt

from nipype import IdentityInterface, Function, Node, Workflow
from nipype.interfaces.base import (BaseInterface,
                                    BaseInterfaceInputSpec,
                                    OutputMultiPath,
                                    TraitedSpec, File, traits)
from nipype.interfaces import fsl, freesurfer
from nipype.utils.filemanip import fname_presuffix

import seaborn as sns
from moss import locator
from moss.mosaic import Mosaic

import lyman

imports = ["import os",
           "import os.path as op",
           "import numpy as np",
           "import scipy as sp",
           "import pandas as pd",
           "import nibabel as nib",
           "import matplotlib as mpl",
           "import matplotlib.pyplot as plt",
           "from skimage import morphology",
           "from nipype.interfaces import fsl",
           "from moss import locator",
           "import seaborn as sns"]


def create_volume_mixedfx_workflow(name="volume_group",
                                   subject_list=None,
                                   regressors=None,
                                   contrasts=None,
                                   exp_info=None):

    # Handle default arguments
    if subject_list is None:
        subject_list = []
    if regressors is None:
        regressors = dict(group_mean=[])
    if contrasts is None:
        contrasts = [["group_mean", "T", ["group_mean"], [1]]]
    if exp_info is None:
        exp_info = lyman.default_experiment_parameters()

    # Define workflow inputs
    inputnode = Node(IdentityInterface(["l1_contrast",
                                        "copes",
                                        "varcopes",
                                        "dofs"]),
                     "inputnode")

    # Merge the fixed effect summary images into one 4D image
    mergecope = Node(fsl.Merge(dimension="t"), "mergecope")
    mergevarcope = Node(fsl.Merge(dimension="t"), "mergevarcope")
    mergedof = Node(fsl.Merge(dimension="t"), "mergedof")

    # Make a simple design
    design = Node(fsl.MultipleRegressDesign(regressors=regressors,
                                            contrasts=contrasts),
                  "design")

    # Find the intersection of masks across subjects
    makemask = Node(Function(["varcope_file"],
                             ["mask_file"],
                             make_group_mask,
                             imports),
                    "makemask")

    # Fit the mixed effects model
    flameo = Node(fsl.FLAMEO(run_mode=exp_info["flame_mode"]), "flameo")

    # Estimate the smoothness of the data
    smoothest = Node(fsl.SmoothEstimate(), "smoothest")

    # Correct for multiple comparisons
    cluster = Node(fsl.Cluster(threshold=exp_info["cluster_zthresh"],
                               pthreshold=exp_info["grf_pthresh"],
                               out_threshold_file=True,
                               out_index_file=True,
                               out_localmax_txt_file=True,
                               peak_distance=exp_info["peak_distance"],
                               use_mm=True),
                   "cluster")

    # Deal with FSL's poorly formatted table of peaks
    peaktable = Node(Function(["localmax_file"],
                              ["out_file"],
                              imports=imports,
                              function=cluster_table),
                     "peaktable")

    # Segment the z stat image with a watershed algorithm
    watershed = Node(Function(["zstat_file", "localmax_file"],
                              ["seg_file", "peak_file", "lut_file"],
                              watershed_segment,
                              imports),
                     "watershed")

    # Sample the zstat image to the surface
    hemisource = Node(IdentityInterface(["mni_hemi"]), "hemisource")
    hemisource.iterables = ("mni_hemi", ["lh", "rh"])

    zstatproj = Node(freesurfer.SampleToSurface(
        sampling_method=exp_info["sampling_method"],
        sampling_range=exp_info["sampling_range"],
        sampling_units=exp_info["sampling_units"],
        smooth_surf=exp_info["surf_smooth"],
        subject_id="fsaverage",
        mni152reg=True,
        target_subject="fsaverage"),
        "zstatproj")

    # Sample the mask to the surface
    maskproj = Node(freesurfer.SampleToSurface(
        sampling_method="max",
        sampling_range=exp_info["sampling_range"],
        sampling_units=exp_info["sampling_units"],
        smooth_surf=exp_info["surf_smooth"],
        subject_id="fsaverage",
        mni152reg=True,
        target_subject="fsaverage"),
        "maskproj")

    # Make static report images in the volume
    report = Node(MFXReport(), "report")
    report.inputs.subjects = subject_list

    # Define the workflow outputs
    outputnode = Node(IdentityInterface(["copes",
                                         "varcopes",
                                         "mask_file",
                                         "flameo_stats",
                                         "thresh_zstat",
                                         "surf_zstat",
                                         "surf_mask",
                                         "cluster_image",
                                         "cluster_peaks",
                                         "seg_file",
                                         "peak_file",
                                         "lut_file",
                                         "report"]),
                      "outputnode")

    # Define and connect up the workflow
    group = Workflow(name)
    group.connect([
        (inputnode, mergecope,
            [("copes", "in_files")]),
        (inputnode, mergevarcope,
            [("varcopes", "in_files")]),
        (inputnode, mergedof,
            [("dofs", "in_files")]),
        (mergecope, flameo,
            [("merged_file", "cope_file")]),
        (mergevarcope, flameo,
            [("merged_file", "var_cope_file")]),
        (mergevarcope, makemask,
            [("merged_file", "varcope_file")]),
        (mergedof, flameo,
            [("merged_file", "dof_var_cope_file")]),
        (makemask, flameo,
            [("mask_file", "mask_file")]),
        (design, flameo,
            [("design_con", "t_con_file"),
             ("design_grp", "cov_split_file"),
             ("design_mat", "design_file")]),
        (flameo, smoothest,
            [("zstats", "zstat_file")]),
        (makemask, smoothest,
            [("mask_file", "mask_file")]),
        (smoothest, cluster,
            [("dlh", "dlh"),
             ("volume", "volume")]),
        (flameo, cluster,
            [("zstats", "in_file")]),
        (cluster, watershed,
            [("threshold_file", "zstat_file"),
             ("localmax_txt_file", "localmax_file")]),
        (makemask, report,
            [("mask_file", "mask_file")]),
        (flameo, report,
            [("zstats", "zstat_file")]),
        (cluster, report,
            [("threshold_file", "zstat_thresh_file"),
             ("localmax_txt_file", "localmax_file")]),
        (mergecope, report,
            [("merged_file", "cope_file")]),
        (watershed, report,
            [("seg_file", "seg_file")]),
        (cluster, peaktable,
            [("localmax_txt_file", "localmax_file")]),
        (cluster, zstatproj,
            [("threshold_file", "source_file")]),
        (hemisource, zstatproj,
            [("mni_hemi", "hemi")]),
        (makemask, maskproj,
            [("mask_file", "source_file")]),
        (hemisource, maskproj,
            [("mni_hemi", "hemi")]),
        (mergecope, outputnode,
            [("merged_file", "copes")]),
        (mergevarcope, outputnode,
            [("merged_file", "varcopes")]),
        (makemask, outputnode,
            [("mask_file", "mask_file")]),
        (flameo, outputnode,
            [("stats_dir", "flameo_stats")]),
        (cluster, outputnode,
            [("threshold_file", "thresh_zstat"),
             ("index_file", "cluster_image")]),
        (peaktable, outputnode,
            [("out_file", "cluster_peaks")]),
        (watershed, outputnode,
            [("seg_file", "seg_file"),
             ("peak_file", "peak_file"),
             ("lut_file", "lut_file")]),
        (zstatproj, outputnode,
            [("out_file", "surf_zstat")]),
        (maskproj, outputnode,
            [("out_file", "surf_mask")]),
        (report, outputnode,
            [("out_files", "report")]),
        ])

    return group, inputnode, outputnode


def make_group_mask(varcope_file):
    """Find the intersection of the MNI brain and var > 0 voxels."""
    mni_mask = fsl.Info.standard_image("MNI152_T1_2mm_brain_mask.nii.gz")
    mni_img = nib.load(mni_mask)
    mask_data = mni_img.get_data().astype(bool)

    # Find the voxels with positive variance
    var_data = nib.load(varcope_file).get_data()
    good_var = var_data.all(axis=-1)

    # Find the intersection
    mask_data *= good_var

    # Save the mask file
    new_img = nib.Nifti1Image(mask_data,
                              mni_img.get_affine(),
                              mni_img.get_header())
    new_img.set_data_dtype(np.int16)
    mask_file = os.path.abspath("group_mask.nii.gz")
    new_img.to_filename(mask_file)
    return mask_file


def watershed_segment(zstat_file, localmax_file):
    """Segment the thresholded zstat image."""
    z_img = nib.load(zstat_file)
    z_data = z_img.get_data()

    # Set up the output filenames
    seg_file = op.basename(zstat_file).replace(".nii.gz", "_seg.nii.gz")
    seg_file = op.abspath(seg_file)
    peak_file = op.basename(zstat_file).replace(".nii.gz", "_peaks.nii.gz")
    peak_file = op.abspath(peak_file)
    lut_file = seg_file.replace(".nii.gz", ".txt")

    # Read in the peak txt file from FSL cluster
    peaks = pd.read_table(localmax_file, "\t")[["x", "y", "z"]].values
    markers = np.zeros_like(z_data)

    # Do the watershed, or not, depending on whether we had peaks
    if len(peaks):
        markers[tuple(peaks.T)] = np.arange(len(peaks)) + 1
        seg = morphology.watershed(-z_data, markers, mask=z_data > 0)
    else:
        seg = np.zeros_like(z_data)

    # Create a Nifti image with the segmentation and save it
    seg_img = nib.Nifti1Image(seg.astype(np.int16),
                              z_img.get_affine(),
                              z_img.get_header())
    seg_img.set_data_dtype(np.int16)
    seg_img.to_filename(seg_file)

    # Create a Nifti image with just the peaks and save it
    peak_img = nib.Nifti1Image(markers.astype(np.int16),
                               z_img.get_affine(),
                               z_img.get_header())
    peak_img.set_data_dtype(np.int16)
    peak_img.to_filename(peak_file)

    # Write a lookup-table in Freesurfer format so we can
    # view the segmentation in Freeview
    n = int(markers.max())
    colors = [[0, 0, 0]] + sns.husl_palette(n)
    colors = np.hstack([colors, np.zeros((n + 1, 1))])
    lut_data = pd.DataFrame(columns=["#ID", "ROI", "R", "G", "B", "A"],
                            index=np.arange(n + 1))
    names = ["Unknown"] + ["roi_%d" % i for i in range(1, n + 1)]
    lut_data["ROI"] = np.array(names)
    lut_data["#ID"] = np.arange(n + 1)
    lut_data.loc[:, "R":"A"] = (colors * 255).astype(int)
    lut_data.to_csv(lut_file, "\t", index=False)

    return seg_file, peak_file, lut_file


class MFXReportInput(BaseInterfaceInputSpec):

    mask_file = File(exits=True)
    zstat_file = File(exsts=True)
    zstat_thresh_file = File(exsts=True)
    localmax_file = File(exists=True)
    cope_file = File(exists=True)
    seg_file = File(exists=True)
    subjects = traits.List()


class MFXReportOutput(TraitedSpec):

    out_files = OutputMultiPath(File(exists=True))


class MFXReport(BaseInterface):

    input_spec = MFXReportInput
    output_spec = MFXReportOutput

    def _run_interface(self, runtime):

        self.out_files = []

        self.save_subject_list()
        self.plot_mask()
        self.plot_full_zstat()
        self.plot_thresh_zstat()

        self.peaks = peaks = self._load_peaks()
        if len(peaks):

            self.plot_watershed(peaks)
            self.plot_peaks(peaks)
            self.plot_boxes(peaks)

        else:
            fnames = [self._png_name(self.inputs.seg_file),
                      self._png_name(self.inputs.zstat_thresh_file, "_peaks"),
                      op.realpath("peak_boxplot.png")]
            self.out_files.extend(fnames)
            for name in fnames:
                with open(name, "wb"):
                    pass

        return runtime

    def save_subject_list(self):
        """Save the subject list for this analysis."""
        subjects_fname = op.realpath("subjects.txt")
        np.savetxt(subjects_fname, self.inputs.subjects, "%s")
        self.out_files.append(subjects_fname)

    def plot_mask(self):
        """Plot the analysis mask."""
        m = Mosaic(stat=self.inputs.mask_file)
        m.plot_mask()
        out_fname = self._png_name(self.inputs.mask_file)
        self.out_files.append(out_fname)
        m.savefig(out_fname)
        m.close()

    def plot_full_zstat(self):
        """Plot the unthresholded zstat."""
        m = Mosaic(stat=self.inputs.zstat_file, mask=self.inputs.mask_file)
        m.plot_overlay(cmap="coolwarm", center=True, alpha=.9)
        out_fname = self._png_name(self.inputs.zstat_file)
        self.out_files.append(out_fname)
        m.savefig(out_fname)
        m.close()

    def plot_thresh_zstat(self):
        """Plot the thresholded zstat."""
        m = Mosaic(stat=self.inputs.zstat_thresh_file,
                   mask=self.inputs.mask_file)
        m.plot_activation(pos_cmap="OrRd_r", vfloor=3.3, alpha=.9)
        out_fname = self._png_name(self.inputs.zstat_thresh_file)
        self.out_files.append(out_fname)
        m.savefig(out_fname)
        m.close()

    def plot_watershed(self, peaks):
        """Plot the watershed segmentation."""
        palette = sns.husl_palette(len(peaks))
        cmap = mpl.colors.ListedColormap(palette)

        m = Mosaic(stat=self.inputs.seg_file, mask=self.inputs.mask_file)
        m.plot_overlay(thresh=.5, cmap=cmap, vmin=1, vmax=len(peaks))
        out_fname = self._png_name(self.inputs.seg_file)
        self.out_files.append(out_fname)
        m.savefig(out_fname)
        m.close()

    def plot_peaks(self, peaks):
        """Plot the peaks."""
        palette = sns.husl_palette(len(peaks))
        cmap = mpl.colors.ListedColormap(palette)

        disk_img = self._peaks_to_disks(peaks)
        m = Mosaic(stat=disk_img, mask=self.inputs.mask_file)
        m.plot_overlay(thresh=.5, cmap=cmap, vmin=1, vmax=len(peaks))
        out_fname = self._png_name(self.inputs.zstat_thresh_file, "_peaks")
        self.out_files.append(out_fname)
        m.savefig(out_fname)
        m.close()

    def plot_boxes(self, peaks):
        """Draw a boxplot to show the distribution of copes at peaks."""
        cope_data = nib.load(self.inputs.cope_file).get_data()
        peak_spheres = self._peaks_to_spheres(peaks).get_data()
        peak_dists = np.zeros((cope_data.shape[-1], len(peaks)))
        for i, peak in enumerate(peaks, 1):
            sphere_mean = cope_data[peak_spheres == i].mean(axis=(0))
            peak_dists[:, i - 1] =  sphere_mean

        with sns.axes_style("whitegrid"):
            f, ax = plt.subplots(figsize=(9, float(len(peaks)) / 3 + 0.33))
        sns.boxplot(peak_dists[::-1], ax=ax, vert=False)
        sns.despine(left=True, bottom=True)
        ax.axvline(0, c=".3", ls="--")
        labels = np.arange(len(peaks))[::-1] + 1
        ax.set(yticklabels=labels, ylabel="Local Maximum", xlabel="COPE Value")

        out_fname = op.realpath("peak_boxplot.png")
        self.out_files.append(out_fname)
        f.savefig(out_fname, bbox_inches="tight")
        plt.close(f)

    def _load_peaks(self):

        peak_df = pd.read_table(self.inputs.localmax_file, "\t")
        peaks = peak_df[["x", "y", "z"]].values
        return peaks

    def _peaks_to_disks(self, peaks, r=4):

        zstat_img = nib.load(self.inputs.zstat_file)

        x, y = np.ogrid[-r:r + 1, -r:r + 1]
        disk = x ** 2 + y ** 2 <= r ** 2
        dilator = np.dstack([disk, disk, np.zeros_like(disk)])
        disk_data = self._dilate_peaks(zstat_img.shape, peaks, dilator)

        disk_img = nib.Nifti1Image(disk_data, zstat_img.get_affine())
        return disk_img

    def _peaks_to_spheres(self, peaks, r=4):

        zstat_img = nib.load(self.inputs.zstat_file)

        x, y, z = np.ogrid[-r:r + 1, -r:r + 1, -r:r + 1]
        dilator = x ** 2 + y ** 2 + z ** 2 <= r ** 2
        sphere_data = self._dilate_peaks(zstat_img.shape, peaks, dilator)

        sphere_img = nib.Nifti1Image(sphere_data, zstat_img.get_affine())
        return sphere_img

    def _dilate_peaks(self, shape, peaks, dilator):

        peak_data = np.zeros(shape)
        disk_data = np.zeros(shape)

        for i, peak in enumerate(peaks, 1):
            spot = np.zeros(shape, np.bool)
            spot[tuple(peak)] = 1
            peak_data += spot
            disk = binary_dilation(spot, dilator)
            disk_data[disk] = i

        return disk_data

    def _png_name(self, fname, suffix=""):
        """Convert a nifti filename to a png filename in this dir."""
        out_fname = fname_presuffix(fname, suffix=suffix + ".png",
                                    newpath=os.getcwd(),
                                    use_ext=False)
        return out_fname

    def _list_outputs(self):

        outputs = self._outputs().get()
        outputs["out_files"] = self.out_files
        return outputs


def cluster_table(localmax_file):
    """Add some info to an FSL cluster file and format it properly."""
    df = pd.read_table(localmax_file, delimiter="\t")
    df = df[["Cluster Index", "Value", "x", "y", "z"]]
    df.columns = ["Cluster", "Value", "x", "y", "z"]
    df.index.name = "Peak"

    # Find out where the peaks most likely are
    if len(df):
        coords = df[["x", "y", "z"]].values
        loc_df = locator.locate_peaks(coords)
        df = pd.concat([df, loc_df], axis=1)
        mni_coords = locator.vox_to_mni(coords).T
        for i, ax in enumerate(["x", "y", "z"]):
            df[ax] = mni_coords[i]

    out_file = op.abspath(op.basename(localmax_file[:-3] + "csv"))
    df.to_csv(out_file)
    return out_file
