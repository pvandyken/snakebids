"""Utilities for converting Snakemake apps to BIDS apps."""

import itertools as it
import json
import logging
import operator as op
import re
from collections import UserDict, defaultdict
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
    cast,
    overload,
)

import attr
import more_itertools as itx
from bids import BIDSLayout, BIDSLayoutIndexer
from cached_property import cached_property
from immutabledict import immutabledict
from typing_extensions import Literal, TypedDict

from snakebids.core.filtering import filter_list
from snakebids.utils.snakemake_io import glob_wildcards
from snakebids.utils.utils import read_bids_tags

_logger = logging.getLogger(__name__)


class BidsDatasetDict(TypedDict):
    """Dict equivalent of BidsInputs, for backwards-compatibility"""

    input_path: Dict[str, str]
    input_zip_lists: Dict[str, Dict[str, List[str]]]
    input_lists: Dict[str, Dict[str, List[str]]]
    input_wildcards: Dict[str, Dict[str, str]]
    subjects: List[str]
    sessions: List[str]
    subj_wildcards: Dict[str, str]


@attr.define
class BidsComponent:
    """Component of a BidsDataset mapping entities to their resolved values

    Properties
    ----------
    input_name
        Name of the component
    input_path
        Wildcard-filled path that matches the files for this component.
    input_zip_lists
        Dictionary where each key is a wildcard entity and each value is a list of the
        values found for that entity. Each of these lists has length equal to the number
        of images matched for this modality, so they can be zipped together to get a
        list of the wildcard values for each file.

    Attributes
    ----------
    input_name
    input_path
    input_zip_lists
    """

    input_name: str = attr.field(on_setattr=attr.setters.frozen)
    input_path: str = attr.field(on_setattr=attr.setters.frozen)
    input_zip_lists: immutabledict[str, List[str]] = attr.field(
        on_setattr=attr.setters.frozen
    )

    _input_lists: Optional[Dict[str, List[str]]] = attr.field(default=None, init=False)
    _input_wildcards: Optional[Dict[str, str]] = attr.field(default=None, init=False)

    @property
    def input_lists(self):
        """Compact list reprentation of values

        Dictionary where each key is a wildcard entity and each value is a list of the
        unique values found for that entity. These lists might not be the same length.
        """
        if self._input_lists is None:
            self._input_lists = {
                entity: list(set(values))
                for entity, values in self.input_zip_lists.items()
            }
        return self._input_lists

    @property
    def input_wildcards(self):
        """Wildcards in brace-wrapped syntax

        Dictionary where each key is the name of a wildcard entity, and each value is
        the Snakemake wildcard used for that entity.
        """
        if self._input_wildcards is None:
            self._input_wildcards = {
                entity: f"{{{entity}}}" for entity in self.input_zip_lists
            }
        return self._input_wildcards

    def __eq__(self, other: Union["BidsComponent", object]):
        if not isinstance(other, BidsComponent):
            return False

        def sorted_items(dictionary: immutabledict[str, List[str]]):
            return sorted(dictionary.items(), key=op.itemgetter(0))

        if set(self.input_zip_lists) != set(other.input_zip_lists):
            return False

        if not other.input_zip_lists and not self.input_zip_lists:
            return True

        other_items = cast(
            List[List[str]], list(zip(*sorted_items(other.input_zip_lists)))[1]
        )
        our_items = cast(
            List[List[str]], list(zip(*sorted_items(self.input_zip_lists)))[1]
        )

        if set(zip(*our_items)) != set(zip(*other_items)):
            return False

        if self.input_path != other.input_path:
            return False

        return self.input_name == other.input_name


if TYPE_CHECKING:
    _BidsComponentsType = UserDict[str, BidsComponent]
else:
    # UserDict is not subscriptable in py37
    _BidsComponentsType = UserDict


class BidsDataset(_BidsComponentsType):
    """A bids dataset parsed by pybids, organized into BidsComponents.

    BidsDatasets are typically generated using `generate_inputs()`, which reads the
    `pybids_inputs` field in your snakemake config file and, for each entry, creates
    a BidsComponent using the provided name, wildcards, and filters.

    Individual components can be accessed using bracket-syntax: (e.g.`inputs["t1w"]`).
    Component access attributes (input_*) along with the component name in brackets can
    also be used. For example, `BidsComponents.input_lists["t1w"]` and
    `BidsComponents["t1w"].input_lists` return the same thing.

    Provides access to summarizing information, for instance, the set of all subjects or
    sessions found in the dataset

    Attributes
    ----------
    input_path
        Dict mapping bids components to their paths.
    input_zip_lists
        Dict mapping bids components to their input_zip_lists
    input_lists
        Dict mapping bids components to their input_lists
    input_wildcards
        Dict mapping bids components to their wildcard dicts
    """

    # pylint: disable=super-init-not-called
    def __init__(self, data: Any):
        self.data = dict(data)

    def __setitem__(self, _: Any, __: Any):
        raise NotImplementedError(
            f"Modification of {self.__class__.__name__} is not yet supported"
        )

    @cached_property
    def input_path(self):
        return {key: value.input_path for key, value in self.data.items()}

    @cached_property
    def input_zip_lists(self):
        return {key: value.input_zip_lists for key, value in self.data.items()}

    @cached_property
    def input_lists(self):
        return {key: value.input_lists for key, value in self.data.items()}

    @cached_property
    def input_wildcards(self):
        return {key: value.input_wildcards for key, value in self.data.items()}

    @cached_property
    def subjects(self):
        """A list of the subjects in the dataset."""
        return [
            *{
                *it.chain.from_iterable(
                    input_list["subject"]
                    for input_list in self.input_lists.values()
                    if "subject" in input_list
                )
            }
        ]

    @property
    # @ft.lru_cache(None)
    def sessions(self):
        """A list of the sessions in the dataset."""
        return [
            *{
                *it.chain.from_iterable(
                    input_list["session"]
                    for input_list in self.input_lists.values()
                    if "session" in input_list
                )
            }
        ]

    @property
    # @ft.lru_cache(None)
    def subj_wildcards(self):
        """The subject and session wildcards applicable to this dataset.

        ``{"subject":"{subject}"}`` if there is only one session, ``{"subject":
        "{subject}", "session": "{session}"}`` if there are multiple sessions.
        """
        if len(self.sessions) == 0:
            return {"subject": "{subject}"}
        return {
            "subject": "{subject}",
            "session": "{session}",
        }

    @property
    def as_dict(self):
        """Get the layout as a legacy dict

        Included primarily for backward compatability with older versions of snakebids,
        where generate_inputs() returned a dict rather than the `BidsDataset` class

        Returns
        -------
        BidsDatasetDict
        """
        return BidsDatasetDict(
            input_path=self.input_path,
            input_lists=self.input_lists,
            input_wildcards=self.input_wildcards,
            input_zip_lists={
                label: dict(values) for label, values in self.input_zip_lists.items()
            },
            subjects=self.subjects,
            sessions=self.sessions,
            subj_wildcards=self.subj_wildcards,
        )

    @classmethod
    def from_iterable(cls, iterable: Iterable[BidsComponent]):
        """Construct Dataset from iterable of BidsComponents

        Parameters
        ----------
        iterable : Iterable[BidsComponent]

        Returns
        -------
        BidsDataset
        """
        return cls({bidsinput.input_name: bidsinput for bidsinput in iterable})


# pylint: disable=too-many-arguments
@overload
def generate_inputs(
    bids_dir,
    pybids_inputs,
    pybids_database_dir=...,
    pybids_reset_database=...,
    derivatives=...,
    pybids_config=...,
    limit_to=...,
    participant_label=...,
    exclude_participant_label=...,
    use_bids_inputs: Union[Literal[False], None] = ...,
) -> BidsDatasetDict:
    ...


# pylint: disable=too-many-arguments
@overload
def generate_inputs(
    bids_dir,
    pybids_inputs,
    pybids_database_dir=...,
    pybids_reset_database=...,
    derivatives=...,
    pybids_config=...,
    limit_to=...,
    participant_label=...,
    exclude_participant_label=...,
    use_bids_inputs: Literal[True] = ...,
) -> BidsDataset:
    ...


# pylint: disable=too-many-arguments
def generate_inputs(
    bids_dir,
    pybids_inputs,
    pybids_database_dir=None,
    pybids_reset_database=False,
    derivatives=False,
    pybids_config=None,
    limit_to=None,
    participant_label=None,
    exclude_participant_label=None,
    use_bids_inputs=None,
):
    """Dynamically generate snakemake inputs using pybids_inputs

    Pybids is used to parse the bids_dir. Custom paths can also be parsed by including
    the custom_paths entry under the pybids_inputs descriptor.

    Parameters
    ----------
    bids_dir : str
        Path to bids directory

    pybids_inputs : dict
        Configuration for bids inputs, with keys as the names (``str``)

        Nested `dicts` with the following required keys:

        * ``"filters"``: Dictionary of entity: "values" (dict of str -> str or list of
          str). The entity keywords should the bids tags on which to filter. The values
          should be an acceptable str value for that entity, or a list of acceptable str
          values.

        * ``"wildcards"``: List of (str) bids tags to include as wildcards in
          snakemake. At minimum this should usually include
          ``['subject','session']``, plus any other wildcards that you may
          want to make use of in your snakemake workflow, or want to retain
          in the output paths. Any wildcards in this list that are not in the
          filename will just be ignored.

        * ``"custom_path"``: Custom path to be parsed with wildcards wrapped in braces,
          as in ``/path/to/sub-{subject}/{wildcard_1}-{wildcard_2}``. This path will be
          parsed without pybids, allowing the use of non-bids-compliant paths.

    pybids_database_dir : str
        Path to database directory. If None is provided, database
        is not used

    pybids_reset_database : bool
        A boolean that determines whether to reset / overwrite
        existing database.

    derivatives : bool
        Indicates whether pybids should look for derivative datasets under bids_dir.
        These datasets must be properly formatted according to bids specs to be
        recognized. Defaults to False.

    limit_to : list of str, optional
        If provided, indicates which input descriptors from pybids_inputs should be
        parsed. For example, if pybids_inputs describes ``"bold"`` and ``"dwi"`` inputs,
        and ``limit_to = ["bold"]``, only the "bold" inputs will be parsed. "dwi" will
        be ignored

    participant_label : str or list of str, optional
        Indicate one or more participants to be included from input parsing. This may
        cause errors if subject filters are also specified in pybids_inputs. It may not
        be specified if exclude_participant_label is specified

    exclude_participant_label : str or list of str, optional
        Indicate one or more participants to be excluded from input parsing. This may
        cause errors if subject filters are also specified in pybids_inputs. It may not
        be specified if participant_label is specified

    use_bids_inputs : bool, optional
        If True, opts in to the new BidsInputs output, otherwise returns the classic
        dict. Currently, the classic dict will be returned by default, however, this
        will change in a future release. If you do not wish to migrate to the new
        BidsInputs, we recommend you explictely set this parameter to False

    Returns
    -------
    BidsInputs or BidsInputsDict:
        Object containing organized information about the bids inputs for consumption
        in snakemake. See the documentation of BidsInputs for details and examples.

    Example
    -------
    As an example, consider the following BIDS dataset::

        bids-example/
        ├── dataset_description.json
        ├── participants.tsv
        ├── README
        └── sub-control01
            ├── anat
            │   ├── sub-control01_T1w.json
            │   ├── sub-control01_T1w.nii.gz
            │   ├── sub-control01_T2w.json
            │   └── sub-control01_T2w.nii.gz
            ├── dwi
            │   ├── sub-control01_dwi.bval
            │   ├── sub-control01_dwi.bvec
            │   └── sub-control01_dwi.nii.gz
            ├── fmap
            │   ├── sub-control01_magnitude1.nii.gz
            │   ├── sub-control01_phasediff.json
            │   ├── sub-control01_phasediff.nii.gz
            │   └── sub-control01_scans.tsv
            └── func
                ├── sub-control01_task-nback_bold.json
                ├── sub-control01_task-nback_bold.nii.gz
                ├── sub-control01_task-nback_events.tsv
                ├── sub-control01_task-nback_physio.json
                ├── sub-control01_task-nback_physio.tsv.gz
                ├── sub-control01_task-nback_sbref.nii.gz
                ├── sub-control01_task-rest_bold.json
                ├── sub-control01_task-rest_bold.nii.gz
                ├── sub-control01_task-rest_physio.json
                └── sub-control01_task-rest_physio.tsv.gz

    With the following ``pybids_inputs`` defined in the config file::

        pybids_inputs:
          bold:
            filters:
              suffix: 'bold'
              extension: '.nii.gz'
              datatype: 'func'
            wildcards:
              - subject
              - session
              - acquisition
              - task
              - run

    Then ``generate_inputs(bids_dir, pybids_input)`` would return the
    following values::

        {
            "input_path": {
                "bold": "bids-example/sub-{subject}/func/sub-{subject}_task-{task}_bold\
                    .nii.gz"
            },
            "input_zip_lists": {
                "bold": {
                    "subject": ["control01", "control01"],
                    "task": ["nback", "rest"]
                }
            },
            "input_lists": {
                "bold": {
                    "subject": ["control01"],
                    "task": ["nback", "rest"]
                }
            },
            "input_wildcards": {
                "bold": {
                    "subject": "{subject}",
                    "task": "{task}"
                }
            },
            "subjects": ["subject01"],
            "sessions": [],
            "subj_wildcards": {"subject": "{subject}"}
        }

    Or, if BidsInputs is enabled::

        <class BidsInputs>
            input_path: {
                "bold": "bids-example/sub-{subject}/func/sub-{subject}_task-{task}_bold\
                    .nii.gz"
            }
            input_zip_lists: {
                "bold": {
                    "subject": ["control01", "control01"],
                    "task": ["nback", "rest"]
                }
            }
            input_lists: {
                "bold": {
                    "subject": ["control01"],
                    "task": ["nback", "rest"]
                }
            }
            input_wildcards: {
                "bold": {
                    "subject": "{subject}",
                    "task": "{task}"
                }
            }
            subjects: ["subject01"]
            sessions: []
            subj_wildcards: {"subject": "{subject}"}
    """

    subject_filter, regex_search = _generate_filters(
        participant_label, exclude_participant_label
    )

    # Generates a BIDSLayout
    layout = _gen_bids_layout(
        bids_dir=bids_dir,
        derivatives=derivatives,
        pybids_config=pybids_config,
        pybids_database_dir=pybids_database_dir,
        pybids_reset_database=pybids_reset_database,
    )

    # this will populate input_path, input_lists, input_zip_lists, and
    # input_wildcards
    filters = {"subject": subject_filter} if subject_filter else {}
    bids_inputs = _get_lists_from_bids(
        bids_layout=layout,
        pybids_inputs=pybids_inputs,
        limit_to=limit_to,
        regex_search=regex_search,
        **(filters),
    )

    if use_bids_inputs is None:
        _logger.warning(
            "The dictionary returned by generate_inputs() will soon be deprecated in "
            "favour of the new BidsInputs class. BidsInputs provides the same "
            "functionality of the dict, but with attribute access and new convience "
            "methods. In a future release, generate_inputs() will return this class "
            "by default. You can opt into this behaviour now by setting the "
            "`use_bids_inputs` argument in generate_inputs() to True. If you do not "
            "wish to migrate your code to BidsInputs at this time, we recommend you "
            "explicately set `use_bids_inputs` to False. This will preserve the "
            "current behaviour of returning a Dict in future releases, and will "
            "silence this warning."
        )
        use_bids_inputs = False
    if use_bids_inputs:
        return BidsDataset.from_iterable(bids_inputs)
    return BidsDataset.from_iterable(bids_inputs).as_dict


def _gen_bids_layout(
    bids_dir,
    derivatives,
    pybids_database_dir,
    pybids_reset_database,
    pybids_config=None,
):
    """Create (or reindex) the BIDSLayout if one doesn't exist,
    which is only saved if a database directory path is provided

     Parameters
    ----------
    bids_dir : str
        Path to bids directory

    derivatives : bool
        A boolean (or path(s) to derivatives datasets) that
        determines whether snakebids will search in the
        derivatives subdirectory of the input dataset.

    pybids_database_dir : str
        Path to database directory. If None is provided, database
        is not used

    pybids_reset_database : bool
        A boolean that determines whether to reset / overwrite
        existing database.

    Returns
    -------
    layout : BIDSLayout
        Layout from pybids for accessing the BIDS dataset to grab paths
    """

    # Set db dir to None (otherwise saves to parent dir)
    if Path(bids_dir):
        # Check for database_dir
        # If blank, assume db not to be used
        if pybids_database_dir == "":
            pybids_database_dir = None
        # Otherwise check for relative path and update
        elif (
            pybids_database_dir is not None
            and not Path(pybids_database_dir).is_absolute()
        ):
            pybids_database_dir = None
            _logger.warning("Absolute path must be provided, database will not be used")

        layout = BIDSLayout(
            bids_dir,
            derivatives=derivatives,
            validate=False,
            config=pybids_config,
            database_path=pybids_database_dir,
            reset_database=pybids_reset_database,
            indexer=BIDSLayoutIndexer(validate=False, index_metadata=False),
        )
    else:
        _logger.info(
            "bids_dir does not exist, skipping PyBIDS and using "
            "custom file paths only"
        )
        layout = None

    return layout


def write_derivative_json(snakemake, **kwargs):
    """Snakemake function to read a json file, and write to a new one,
    adding BIDS derivatives fields for Sources and Parameters.

    Parameters
    ----------
    snakemake : struct Snakemake
        structure passed to snakemake python scripts, containing input,
        output, params, config ...
        This function requires input.json and output.json to be defined, as
        it will read and write json files
    """

    with open(snakemake.input.json, "r", encoding="utf-8") as input_json:
        sidecar = json.load(input_json)

    sidecar.update(
        {
            "Sources": [snakemake.input],
            "Parameters": snakemake.params,
            **kwargs,
        }
    )

    with open(snakemake.output.json, "w", encoding="utf-8") as outfile:
        json.dump(sidecar, outfile, indent=4)


def _generate_filters(
    include: Union[List[str], str, None] = None,
    exclude: Union[List[str], str, None] = None,
) -> Tuple[List[str], bool]:
    """Generate Pybids filter based on inclusion or exclusion criteria

    Converts either a list of values to include or exclude in a list of Pybids
    compatible filters. Unlike inclusion values, exclusion requires regex filtering. The
    necessity for regex will be indicated by the boolean value of the second returned
    item: True if regex is needed, False otherwise. Raises an exception if both include
    and exclude are stipulated

    Parameters
    ----------
    include : list of str or str, optional
        Values to include, values not found in this list will be excluded, by default
        None
    exclude : list of str or str, optional
        Values to exclude, only values not found in this list will be included, by
        default None

    Returns
    -------
    list of str, bool
        Two values: the first, a list of pybids compatible filters; the second, a
        boolean indicating whether regex_search must be enabled in pybids

    Raises
    ------
    ValueError Raised of both include and exclude values are stipulated.
    """
    if include is not None and exclude is not None:
        raise ValueError(
            "Cannot define both participant_label and "
            "exclude_participant_label at the same time"
        )

    # add participant_label or exclude_participant_label to search terms (if
    # defined)
    # we make the item key in search_terms a list so we can have both
    # include and exclude defined
    if include is not None:
        return [*itx.always_iterable(include)], False

    if exclude is not None:
        # if multiple items to exclude, combine with with item1|item2|...
        if isinstance(exclude, list):
            exclude_string = "|".join(re.escape(label) for label in exclude)
        # if not, then string is the label itself
        else:
            exclude_string = re.escape(exclude)
        # regex to exclude subjects
        return [f"^((?!({exclude_string})$).*)$"], True
    return [], False


def _parse_custom_path(
    input_path: Union[Path, str],
    regex_search: bool = False,
    **filters: Union[List[str], str],
) -> immutabledict[str, List[str]]:
    """Glob wildcards from a custom path and apply filters

    This replicates pybids path globbing for any custom path. Input path should have
    wildcards in braces as in "path/of/{wildcard_1}/{wildcard_2}_{wildcard_3}" Output
    will be arranged into a zip list of matches, list of matches, and Snakemake wildcard
    for each wildcard.

    Note that, currently, this will get confused if wildcard content matches
    non-wildcard content. For example, considering the path template above, the example
    "path/of/var1/variable_2_var3" would bug out because of the extra underscore.

    Parameters
    ----------
    input_path : str
        Path to be globbed
    regex_search : bool
        If True, use regex matching for filtering rather than simple equality
    **filters : str or list of str
        Values to keep. Each argument is the name of the entity to search

    Returns
    -------
    input_zip_list, input_list, input_wildcards
    """
    wildcards = glob_wildcards(input_path)
    wildcard_names = list(wildcards._fields)

    if len(wildcard_names) == 0:
        _logger.warning("No wildcards defined in %s", input_path)

    # Initialize output values
    input_zip_lists: Dict[str, List[str]] = {}

    # Log an error if no matches found
    # TODO: This will fail to detect filtering correctly as, up till now, it has
    #       only been performed on input_lists
    if len(wildcards[0]) == 0:
        _logger.error("No matching files for %s", input_path)
        return immutabledict()

    # Loop through every wildcard name
    for i, wildcard in enumerate(wildcard_names):
        # Check if this wildcard needs to be filtered
        # Add the wildcard item to each output value, using filtering for input_lists
        input_zip_lists[wildcard] = wildcards[i]

    # Return the output values, running filtering on the input_zip_lists
    return filter_list(input_zip_lists, filters, regex_search=regex_search)


def _parse_bids_path(path: str, wildcards: Iterable[str]) -> Tuple[str, Dict[str, str]]:
    """Replace parameters in an bids path with the given wildcard {tags}.

    Parameters
    ----------
    path : str
        BIDS path
        (e.g. "root/sub-01/ses-01/sub-01_ses-01_T1w.nii.gz")
    wildcards : iterable of str
        BIDS entities to replace with wildcards. (e.g. "subject", "session", "suffix")

    Returns
    -------
    path : str
        Original path with the original entities replaced with wildcards.
        (e.g. "root/sub-{subject}/ses-{session}/sub-{subject}_ses-{session}_{suffix}")
    matches : iterable of (wildcard, value)
        The values matched with each wildcard
    """

    wildcard_values: Dict[str, str] = {}
    bids_tags = read_bids_tags()

    for wildcard in wildcards:
        # Iterate over wildcards, slowly updating the path as each entity is replaced

        tag = bids_tags[wildcard] if wildcard in bids_tags else wildcard

        # this changes e.g. sub-001 to sub-{subject} in the path
        # (so snakemake can use the wildcards)
        # HACK FIX FOR acq vs acquisition etc -- should
        # eventually update the bids() function to also use
        # bids_tags.json, where e.g. acquisition -> acq is
        # defined.. -- then, can use wildcard_name instead
        # of out_name..
        if wildcard not in ["subject", "session"]:
            out_name = tag
        else:
            out_name = wildcard

        if wildcard == "suffix":
            # capture suffix
            match = re.search(r".*_([a-zA-Z0-9]+).*$", path)

            # capture "(before)suffix(after)" and replace with "before{suffix}after"
            new_path = re.sub(r"(.*_)[a-zA-Z0-9]+(.*)$", rf"\1{{{out_name}}}\2", path)

        else:
            pattern = f"{tag}-([a-zA-Z0-9]+)"
            replace = f"{tag}-{{{out_name}}}"

            match = re.search(pattern, path)
            new_path = re.sub(pattern, replace, path)

        if match and match.group(1):
            entity = match.group(1)
        else:
            entity = ""

        # update the path with the {wildcards} -- uses the
        # value from the string (not from the pybids
        # entities), since that has issues with integer
        # formatting (e.g. for run=01)

        path = new_path
        wildcard_values[out_name] = entity

    return path, wildcard_values


def _get_lists_from_bids(
    bids_layout: Optional[BIDSLayout], pybids_inputs, limit_to=None, **filters
) -> Generator[BidsComponent, None, None]:
    """Grabs files using pybids and creates snakemake-friendly lists

    Parameters
    ----------
    bids_layout : BIDSLayout
        Layout from pybids for accessing the BIDS dataset to grab paths

    pybids_inputs : dict
        Dictionary indexed by modality name, specifying the filters and
        wildcards for each pybids input.

    limit_to : list, optional
        List of inputs to skip, this used by snakebids to exclude modalities
        based on cmd-line args

    filters : dict of str -> str or list of str, optional
        Pybids filters to apply globally to all inputs.

    Yields
    ------
    BidsLists:
        One BidsLists is yielded for each modality described by ``pybids_inputs``.
    """
    if limit_to is None:
        limit_to = pybids_inputs.keys()

    for input_name in limit_to:
        _logger.debug("Grabbing inputs for %s...", input_name)

        if "custom_path" in pybids_inputs[input_name].keys():
            # a custom path was specified for this input, skip pybids:
            # get input_wildcards by parsing path for {} entries (using a set
            # to get unique only)
            # get input_zip_lists by using glob_wildcards (but need to modify
            # to deal with multiple wildcards

            input_path: str = pybids_inputs[input_name]["custom_path"]
            input_zip_lists = _parse_custom_path(
                input_path, **pybids_inputs[input_name]["filters"], **filters
            )
            yield BidsComponent(input_name, input_path, input_zip_lists)
            continue

        if bids_layout is None:
            _logger.warning(
                "No valid bids dir given, but %s does not have a custom_path specified "
                "and will be skipped.",
                input_name,
            )
            continue

        input_zip_lists = defaultdict(list)
        paths = set()
        for img in bids_layout.get(
            **pybids_inputs[input_name].get("filters", {}), **filters
        ):
            wildcards: List[str] = [
                wildcard
                for wildcard in pybids_inputs[input_name].get("wildcards", [])
                if wildcard in img.get_entities()
            ]
            _logger.debug("Wildcards %s found entities for %s", wildcards, img.path)

            input_path, parsed_wildcards = _parse_bids_path(img.path, wildcards)

            for wildcard_name, value in parsed_wildcards.items():
                input_zip_lists[wildcard_name].append(value)

            paths.add(input_path)

        # now, check to see if unique
        if len(paths) == 0:
            _logger.warning("No images found for %s", input_name)
            continue
        if len(paths) > 1:
            _logger.warning(
                "More than one snakemake filename for %s, taking the "
                "first. To correct this, use the --filter_%s option to "
                "narrow the search. Found filenames: %s",
                input_name,
                input_name,
                paths,
            )

        input_path = list(paths)[0]

        yield BidsComponent(input_name, input_path, immutabledict(input_zip_lists))


def get_wildcard_constraints(image_types):
    """Return a wildcard_constraints dict for snakemake to use, containing
    all the wildcards that are in the dynamically grabbed inputs

    Parameters
    ----------
    image_types : dict

    Returns
    -------
        Dict containing wildcard constraints for all wildcards in the
        inputs, with typical bids naming constraints, ie letters and numbers
        ``[a-zA-Z0-9]+``.
    """
    bids_constraints = "[a-zA-Z0-9]+"
    return {
        entity: bids_constraints
        for imgtype in image_types.keys()
        for entity in image_types[imgtype].keys()
    }
