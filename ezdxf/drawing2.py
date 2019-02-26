# Created: 11.03.2011
# Copyright (c) 2011-2018, Manfred Moitzi
# License: MIT License
from typing import TYPE_CHECKING, TextIO, Iterable, Union, Sequence, Tuple
from datetime import datetime
import io
import logging
from itertools import chain

from ezdxf.lldxf.const import acad_release, BLK_XREF, BLK_EXTERNAL, DXFValueError, acad_release_to_dxf_version
from ezdxf.lldxf.const import DXF13, DXF14, DXF2007, DXF12, DXF2013, versions_supported_by_save
from ezdxf.lldxf.const import DXFVersionError
from ezdxf.lldxf.loader import load_dxf_structure, fill_database2
from ezdxf.lldxf import repair

from ezdxf.entitydb import EntityDB
from ezdxf.entities.factory import EntityFactory
from ezdxf.layouts.layouts import Layouts
from ezdxf.tools.codepage import tocodepage, toencoding
from ezdxf.sections.sections2 import Sections
from ezdxf.tools.juliandate import juliandate

from ezdxf.tools import guid
from ezdxf.tracker import Tracker
from ezdxf.query import EntityQuery
from ezdxf.groupby import groupby
from ezdxf.render.dimension import DimensionRenderer

from ezdxf.entities.dxfgroups import GroupCollection
from ezdxf.entities.material import MaterialCollection
from ezdxf.entities.mleader import MLeaderStyleCollection
from ezdxf.entities.mline import MLineStyleCollection

logger = logging.getLogger('ezdxf')

if TYPE_CHECKING:
    from ezdxf.eztypes2 import HandleGenerator, DXFTag, SectionDict, SectionType, Table, ViewportTable
    from ezdxf.eztypes2 import HeaderSection, BlocksSection, Dictionary, BlockLayout, Layout
    from ezdxf.eztypes2 import DXFEntity, Layer, DXFLayout, BlockRecord
    LayoutType = Union[Layout, BlockLayout]


class Drawing:
    def __init__(self, dxfversion=DXF2013):
        self.entitydb = EntityDB()
        self.dxffactory = EntityFactory(self)
        self.tracker = Tracker()  # still required

        # Targeted DXF version, but drawing could be exported as any DXF version.
        # If target version is set, it is possible to warn user, if they try to use unsupported features, where they
        # use it and not at exporting, where the location of the code who created that features is not known.
        target_dxfversion = dxfversion.upper()
        self._dxfversion = acad_release_to_dxf_version.get(target_dxfversion, target_dxfversion)
        self.encoding = 'cp1252'
        self.filename = None  # type: str # read/write
        self.sections = None  # type: Sections
        self.rootdict = None  # type: Dictionary
        self.layouts = None  # type: Layouts
        self.groups = None  # type: GroupCollection  # read only
        self.materials = None  # type: MaterialCollection # read only
        self.mleader_styles = None  # type: MLeaderStyleCollection # read only
        self.mline_styles = None  # type: MLineStyleCollection # read only
        self._acad_compatible = True  # will generated DXF file compatible with AutoCAD
        self._dimension_renderer = DimensionRenderer()  # set DIMENSION rendering engine
        self._acad_incompatibility_reason = set()  # avoid multiple warnings for same reason
        # Don't create any new entities here:
        # New created handles could collide with handles loaded from DXF file.
        assert len(self.entitydb) == 0

    @classmethod
    def new(cls, dxfversion=DXF2013) -> 'Drawing':
        doc = Drawing(dxfversion)
        doc._setup()
        return doc

    def _get_encoding(self):
        codepage = self.header.get('$DWGCODEPAGE', 'ANSI_1252')
        return toencoding(codepage)

    def _setup(self):
        self.sections = Sections(self)
        self.rootdict = self.objects.rootdict
        self.objects.setup_objects_management_tables(self.rootdict)  # create missing tables
        self.layouts = Layouts.setup(self)
        self._finalize_setup()

    def _finalize_setup(self):
        """ Common setup tasks for new and loaded DXF drawings. """
        self.groups = GroupCollection(self)
        self.materials = MaterialCollection(self)
        self.mleader_styles = MLeaderStyleCollection(self)
        self.mline_styles = MLineStyleCollection(self)
        # all required internal structures are ready
        # now do the stuff to please AutoCAD
        self._create_required_table_entries()
        self._set_required_layer_attributes()
        self._setup_metadata()

    def _create_required_table_entries(self):
        self._create_required_vports()
        self._create_required_linetypes()
        self._create_required_layers()
        self._create_required_styles()
        self._create_required_appids()
        self._create_required_dimstyles()

    def _set_required_layer_attributes(self):
        for layer in self.layers:  # type: Layer
            layer.set_required_attributes()

    def _create_required_vports(self):
        if '*Active' not in self.viewports:
            self.viewports.new('*Active')

    def _create_required_appids(self):
        if 'ACAD' not in self.appids:
            self.appids.new('ACAD')

    def _create_required_linetypes(self):
        linetypes = self.linetypes
        for name in ('ByBlock', 'ByLayer', 'Continuous'):
            if name not in linetypes:
                linetypes.new(name)

    def _create_required_dimstyles(self):
        if 'Standard' not in self.dimstyles:
            self.dimstyles.new('Standard')

    def _create_required_styles(self):
        if 'Standard' not in self.styles:
            self.styles.new('Standard')

    def _create_required_layers(self):
        layers = self.layers
        if '0' not in layers:
            layers.new('0')
        if 'Defpoints' not in layers:
            layers.new('Defpoints', dxfattribs={'plot': 0})  # do not plot

    def _setup_metadata(self):
        self.header['$ACADVER'] = self.dxfversion
        self.header['$TDCREATE'] = juliandate(datetime.now())
        self.reset_fingerprintguid()
        self.reset_versionguid()

    @property
    def dxfversion(self):
        return self._dxfversion

    @dxfversion.setter
    def dxfversion(self, version):
        version = version.upper()
        version = acad_release_to_dxf_version.get(version, version)  # translates 'R12' -> 'AC1009'
        if version not in versions_supported_by_save:
            raise DXFVersionError("Can not save DXF drawings as DXF version '{}'.".format(version))
        self._dxfversion = version
        self.header['$ACADVER'] = version

    def which_dxfversion(self, dxfversion=None) -> str:
        dxfversion = dxfversion if dxfversion is not None else self.dxfversion
        return acad_release_to_dxf_version.get(dxfversion, dxfversion)

    @classmethod
    def read(cls, stream: TextIO, legacy_mode: bool = False) -> 'Drawing':
        """ Open an existing drawing. """
        from .lldxf.tagger import low_level_tagger, tag_compiler

        tagger = low_level_tagger(stream)
        if legacy_mode:
            tagger = repair.tag_reorder_layer(tagger)
        tagreader = tag_compiler(tagger)
        doc = Drawing()
        doc._load(tagreader)
        return doc

    @classmethod
    def from_tags(cls, compiled_tags: Iterable['DXFTag']) -> 'Drawing':
        doc = Drawing()
        doc._load(compiled_tags)
        return doc

    def _load(self, tagger: Iterable['DXFTag']):
        def get_header(sections: 'SectionDict') -> 'SectionType':
            from .sections.header import HeaderSection
            header_entities = sections.get('HEADER', [None])[0]  # all tags in the first DXF structure entity
            if header_entities is None:  # create default header
                return HeaderSection.new(dxfversion=DXF12)  # file without header are by default DXF R12
            else:
                return HeaderSection.load(header_entities)

        sections = load_dxf_structure(tagger)  # load complete DXF entity structure
        # create section HEADER
        header = get_header(sections)
        self._dxfversion = header.get('$ACADVER', DXF12)  # type: str # read only  # no $ACADVER -> DXF R12
        self.encoding = toencoding(header.get('$DWGCODEPAGE', 'ANSI_1252'))  # type: str # read/write
        # get handle seed
        seed = header.get('$HANDSEED', str(self.entitydb.handles))  # type: str
        # setup handles
        self.entitydb.handles.reset(seed)
        # store all necessary DXF entities in the drawing database
        fill_database2(sections, self.dxffactory)
        # all handles used in the DXF file are known at this point
        # create sections: TABLES, BLOCKS, ENTITIES, CLASSES, OBJECTS
        self.sections = Sections(doc=self, sections=sections, header=header)

        if self.dxfversion == DXF12:
            self.sections.tables.create_table_handles()
            # TABLE requires in DXF12 no handle and has no owner tag, but DXF R2000+, requires a TABLE with handle
            # and each table entry has an owner tag, pointing to the TABLE entry
            # todo: assign each TABLE entity a handle, which is only now possible, when all used handles in the DXF file are known
            # todo: assign all TABLE entries the new handle of TABLE as new owner tag.

            # All entities have handles, despite DXF R12 works also without handles, ezdxf need it.
            pass  # todo: for r12 create block_records and rename $Model_Space and $Paper_Space before Layout setup

        self.rootdict = self.objects.rootdict
        self.objects.setup_objects_management_tables(self.rootdict)  # create missing tables

        # some applications don't setup properly the model and paper space layouts
        # repair.setup_layouts(self)
        self.layouts = Layouts.load(self)
        self._finalize_setup()

    def saveas(self, filename, encoding=None, dxfversion=None):
        dxfversion = self.which_dxfversion(dxfversion)
        self.filename = filename
        self.save(encoding=encoding, dxfversion=dxfversion)

    def save(self, encoding=None, dxfversion=None):
        # DXF R12, R2000, R2004 - ASCII encoding
        # DXF R2007 and newer - UTF-8 encoding
        dxfversion = self.which_dxfversion(dxfversion)

        if encoding is None:
            enc = 'utf-8' if dxfversion >= DXF2007 else self.encoding
        else:  # override default encoding, for applications that handles encoding different than AutoCAD
            enc = encoding
        # in ASCII mode, unknown characters will be escaped as \U+nnnn unicode characters.

        with io.open(self.filename, mode='wt', encoding=enc, errors='dxfreplace') as fp:
            self.write(fp, dxfversion)

    def write(self, stream, dxfversion=None):
        dxfversion = self.which_dxfversion(dxfversion)

        from .lldxf.tagwriter import TagWriter
        if dxfversion == DXF12:
            handles = bool(self.header['$HANDLING'])
        else:
            handles = True
        if dxfversion > DXF12:
            self._register_required_classes(dxfversion)

        self._create_appids()
        self._update_metadata()
        tagwriter = TagWriter(stream, write_handles=handles, dxfversion=dxfversion)
        self.sections.export_dxf(tagwriter)

    def _register_required_classes(self, dxfversion):
        self.sections.classes.add_required_classes(dxfversion)
        for dxftype in self.tracker.dxftypes:
            self.sections.classes.add_class(dxftype)

    def _update_metadata(self):
        now = datetime.now()
        self.header['$TDUPDATE'] = juliandate(now)
        self.header['$HANDSEED'] = str(self.entitydb.next_handle())
        self.header['$DWGCODEPAGE'] = tocodepage(self.encoding)
        self.reset_versionguid()

    def _create_appids(self):
        def create_appid_if_not_exist(name, flags=0):
            if name not in self.appids:
                self.appids.new(name, {'flags': flags})

        if 'HATCH' in self.tracker.dxftypes:
            create_appid_if_not_exist('HATCHBACKGROUNDCOLOR', 0)

    @property
    def acad_release(self) -> str:
        return acad_release.get(self.dxfversion, "unknown")

    @property
    def header(self) -> 'HeaderSection':
        return self.sections.header

    @property
    def blocks(self) -> 'BlocksSection':
        return self.sections.blocks

    @property
    def entities(self):
        return self.sections.entities

    @property
    def objects(self):
        return self.sections.objects

    @property
    def layers(self) -> 'Table':
        return self.sections.tables.layers

    @property
    def linetypes(self) -> 'Table':
        return self.sections.tables.linetypes

    @property
    def styles(self) -> 'Table':
        return self.sections.tables.styles

    @property
    def dimstyles(self) -> 'Table':
        return self.sections.tables.dimstyles

    @property
    def ucs(self) -> 'Table':
        return self.sections.tables.ucs

    @property
    def appids(self) -> 'Table':
        return self.sections.tables.appids

    @property
    def views(self) -> 'Table':
        return self.sections.tables.views

    @property
    def block_records(self) -> 'Table':
        return self.sections.tables.block_records

    @property
    def viewports(self) -> 'ViewportTable':
        return self.sections.tables.viewports

    @property
    def plotstyles(self) -> 'Dictionary':
        return self.rootdict['ACAD_PLOTSTYLENAME']

    @property
    def dimension_renderer(self) -> DimensionRenderer:
        return self._dimension_renderer

    @dimension_renderer.setter
    def dimension_renderer(self, renderer: DimensionRenderer) -> None:
        """
        Set your own dimension line renderer if needed.

        see also: ezdxf.render.dimension

        """
        self._dimension_renderer = renderer

    def modelspace(self) -> 'Layout':
        """ Returns the modelspace layout, displayed as 'Model' tab in CAD applications, defined by block record name
        '*Model_Space'.
        """
        return self.layouts.modelspace()

    def layout(self, name: str = None) -> 'Layout':
        """ Returns paperspace layout `name` or returns first layout in tab order if `name` is None. """
        return self.layouts.get(name)

    def active_layout_name(self) -> str:
        """ Returns the active paperspace layout name, name as displayed in tabs of CAD applications, defined by block
        record name '*Paper_Space'
        """
        active_layout_block_record = self.block_records.get('*Paper_Space')  # type: BlockRecord # block names are case insensitive
        dxf_layout = active_layout_block_record.dxf.layout  # type: DXFLayout
        return dxf_layout.dxf.name

    def active_layout(self) -> 'Layout':
        """ Returns the active paperspace layout, defined by block record name '*Paper_Space' """
        return self.layouts.get(self.active_layout_name())

    def layout_names(self) -> Iterable[str]:
        """ Returns all layout names (modelspace included) in arbitrary order. """
        return list(self.layouts.names())

    def layout_names_in_taborder(self) -> Iterable[str]:
        """ Returns all layout names (modelspace included) in tab order. """
        return list(self.layouts.names_in_taborder())

    def reset_fingerprintguid(self):
        self.header['$FINGERPRINTGUID'] = guid()

    def reset_versionguid(self):
        self.header['$VERSIONGUID'] = guid()


class DrawingX(Drawing):
    """
    The Central Data Object
    """

    @property
    def acad_compatible(self) -> bool:
        return self._acad_compatible

    def add_acad_incompatibility_message(self, msg: str):
        self._acad_compatible = False
        if msg not in self._acad_incompatibility_reason:
            self._acad_incompatibility_reason.add(msg)
            logger.warning('Drawing is incompatible to AutoCAD, because {}.'.format(msg))

    @property
    def _handles(self) -> 'HandleGenerator':
        return self.entitydb.handles

    def delete_layout(self, name):
        if name not in self.layouts:
            raise DXFValueError("Layout '{}' does not exist.".format(name))
        else:
            self.layouts.delete(name)

    def new_layout(self, name, dxfattribs=None):
        if name in self.layouts:
            raise DXFValueError("Layout '{}' already exists.".format(name))
        else:
            return self.layouts.new(name, dxfattribs)

    def layouts_and_blocks(self) -> Iterable['LayoutType']:
        """
        Iterate over all layouts (mode space and paper space) and all block definitions.

        Returns: yields Layout() objects

        """
        return iter(self.blocks)

    def chain_layouts_and_blocks(self) -> Iterable['DXFEntity']:
        """
        Chain entity spaces of all layouts and blocks. Yields an iterator for all entities in all layouts and blocks.

        Returns: yields all entities as DXFEntity() objects

        """
        layouts = list(self.layouts_and_blocks())
        return chain.from_iterable(layouts)

    def get_active_entity_space_layout_keys(self) -> Sequence[str]:
        layout_keys = [self.modelspace().layout_key]
        active_layout_key = self.layouts.get_active_layout_key()
        if active_layout_key is not None:
            layout_keys.append(active_layout_key)
        return layout_keys

    def get_dxf_entity(self, handle: str) -> 'DXFEntity':
        return self.entitydb[handle]

    def add_image_def(self, filename: str, size_in_pixel: Tuple[int, int], name=None):
        """
        Add an image definition to the objects section.

        For AutoCAD works best with absolute image paths but not good, you have to update external references manually
        in AutoCAD, which is not possible in TrueView. If you drawing units differ from 1 meter, you also have to use:
        Drawing.set_raster_variables().

        Args:
            filename: image file name (absolute path works best for AutoCAD)
            size_in_pixel: image size in pixel as (x, y) tuple
            name: image name for internal use, None for using filename as name (best for AutoCAD)

        """

        if 'ACAD_IMAGE_VARS' not in self.rootdict:
            self.objects.set_raster_variables(frame=0, quality=1, units=3)
        if name is None:
            name = filename
        return self.objects.add_image_def(filename, size_in_pixel, name)

    def set_raster_variables(self, frame=0, quality=1, units='m'):
        """
        Set raster variables.

        Args:
            frame: 0 = do not show image frame; 1 = show image frame
            quality: 0 = draft; 1 = high
            units: units for inserting images. This is what one drawing unit is equal to for the purpose of inserting
                   and scaling images with an associated resolution

                   'mm' = Millimeter
                   'cm' = Centimeter
                   'm' = Meter (ezdxf default)
                   'km' = Kilometer
                   'in' = Inch
                   'ft' = Foot
                   'yd' = Yard
                   'mi' = Mile
                   everything else is None

        """
        self.objects.set_raster_variables(frame=frame, quality=quality, units=units)

    def set_wipeout_variables(self, frame=0):
        """
        Set wipeout variables.

        Args:
            frame: 0 = do not show image frame; 1 = show image frame

        """
        self.objects.set_wipeout_variables(frame=frame)

    def add_underlay_def(self, filename: str, format: str = 'ext', name: str = None):
        """
        Add an underlay definition to the objects section.

        Args:
            format: file format as string pdf|dwf|dgn or ext=get format from filename extension
            name: underlay name, None for an auto-generated name

        """
        if format == 'ext':
            format = filename[-3:]
        return self.objects.add_underlay_def(filename, format, name)

    def add_xref_def(self, filename, name, flags=BLK_XREF | BLK_EXTERNAL):
        """
        Add an external reference (xref) definition to the blocks section.

        Add xref to a layout by `layout.add_blockref(name, insert=(0, 0))`.

        Args:
            filename: external reference filename
            name: name of the xref block
            flags: block flags

        """
        self.blocks.new(name=name, dxfattribs={
            'flags': flags,
            'xref_path': filename
        })

    def saveas(self, filename, encoding=None):
        self.filename = filename
        self.save(encoding=encoding)

    def save(self, encoding=None):
        # DXF R12, R2000, R2004 - ASCII encoding
        # DXF R2007 and newer - UTF-8 encoding
        if encoding is None:
            enc = 'utf-8' if self.dxfversion >= 'AC1021' else self.encoding
        else:  # override default encoding, for applications that handles encoding different than AutoCAD
            enc = encoding
        # in ASCII mode, unknown characters will be escaped as \U+nnnn unicode characters.
        with io.open(self.filename, mode='wt', encoding=enc, errors='dxfreplace') as fp:
            self.write(fp)

    def write(self, stream):
        from .lldxf.tagwriter import TagWriter
        if self.dxfversion == 'AC1009':
            handles = bool(self.header['$HANDLING'])
        else:
            handles = True
        if self.dxfversion > 'AC1009':
            self._register_required_classes()
            if self.dxfversion < 'AC1018':
                # remove unsupported group code 91
                repair.fix_classes(self)

        self._create_appids()
        self._update_metadata()
        tagwriter = TagWriter(stream, write_handles=handles)
        self.sections.write(tagwriter)

    def query(self, query='*'):
        """
        Entity query over all layouts and blocks.

        Excluding the OBJECTS section!

        Args:
            query: query string

        Returns: EntityQuery() container

        """
        return EntityQuery(self.chain_layouts_and_blocks(), query)

    def groupby(self, dxfattrib="", key=None):
        """
        Groups DXF entities of all layouts and blocks by an DXF attribute or a key function.

        Excluding the OBJECTS section!

        Args:
            dxfattrib: grouping DXF attribute like 'layer'
            key: key function, which accepts a DXFEntity as argument, returns grouping key of this entity or None for ignore
                 this object. Reason for ignoring: a queried DXF attribute is not supported by this entity

        Returns: dict

        """
        return groupby(self.chain_layouts_and_blocks(), dxfattrib, key)

    def cleanup(self, groups=True):
        """
        Cleanup drawing. Call it before saving the drawing but only if necessary, the process could take a while.

        Args:
            groups (bool): removes deleted and invalid entities from groups
        """
        if groups and self.groups is not None:
            self.groups.cleanup()

    def auditor(self):
        """
        Get auditor for this drawing.

        Returns:
            Auditor() object

        """
        from ezdxf.audit.auditor import Auditor
        return Auditor(self)

    def validate(self, print_report=True):
        """
        Simple way to run an audit process.

        Args:
            print_report: print report to stdout

        Returns: True if no errors occurred else False

        """
        auditor = self.auditor()
        result = list(auditor.filter_zero_pointers(auditor.run()))
        if len(result):
            if print_report:
                auditor.print_report()
            return False
        else:
            return True

    def update_class_instance_counters(self):
        if 'classes' in self.sections:
            self._register_required_classes()
            self.sections.classes.update_instance_counters()
