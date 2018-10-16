"""
Definition of the SqliteCaseReader.
"""
from __future__ import print_function, absolute_import

import re
import sqlite3
from collections import OrderedDict

from six import PY2, PY3, iteritems
from six.moves import range

# import numpy as np

# from openmdao.recorders.base_case_reader import BaseCaseReader
from openmdao.recorders.case_new import Case, PromotedToAbsoluteMap

from openmdao.utils.record_util import check_valid_sqlite3_db, convert_to_np_array
from openmdao.recorders.sqlite_recorder import format_version

from pprint import pprint
# import json

if PY2:
    import cPickle as pickle
    from openmdao.utils.general_utils import json_loads_byteified as json_loads
elif PY3:
    import pickle
    from json import loads as json_loads


# Regular expression used for splitting iteration coordinates, removes separator and iter counts
_coord_split_re = re.compile('\|\\d+\|*')

# regular expression used to determine if a node in an iteration coordinate represents a system
_coord_system_re = re.compile('(_solve_nonlinear|_apply_nonlinear|_solve_linear|_apply_linear)$')


def CaseReader(filename, pre_load=True):
    """
    Return a CaseReader for the given file.

    Parameters
    ----------
    filename : str
        A path to the recorded file.  The file should have been recorded using
        either the SqliteRecorder or the HDF5Recorder.
    pre_load : bool
        If True, load all the data into memory during initialization.

    Returns
    -------
    reader : BaseCaseReader
        An instance of a SqliteCaseReader that is reading filename.
    """
    reader = SqliteCaseReader(filename, pre_load)
    return reader


class BaseCaseReader(object):
    """
    Base class of all CaseReader implementations.

    Attributes
    ----------
    _format_version : int
        The version of the format assumed when loading the file.
    problem_metadata : dict
        Metadata about the problem, including the system hierachy and connections.
    system_metadata : dict
        Metadata about each system in the recorded model, including options and scaling factors.
    """

    def __init__(self, filename, pre_load=False):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The path to the file containing the recorded data.
        pre_load : bool
            If True, load all the data into memory during initialization.
        """
        self._format_version = None
        self.problem_metadata = {}
        self.system_metadata = {}

    def get_cases(self, source, recurse=True, flat=False):
        """
        Initialize.

        Parameters
        ----------
        source : {'problem', 'driver', iteration_coordinate}
            Identifies which cases to return. 'iteration_coordinate' can refer to
            a system or a solver hierarchy location. Defaults to 'problem'.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy.
        flat : bool
            If True, return a flat dictionary rather than a nested dictionary.

        Returns
        -------
        dict
            The cases identified by the source
        """
        pass

    def get_case(self, id, recurse=True):
        """
        Initialize.

        Parameters
        ----------
        id : str
            The unique identifier of the case to return.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy.

        Returns
        -------
        dict
            The case identified by the is
        """
        pass

    def list_sources(self):
        """
        List of all the different recording sources for which there is recorded data.

        Returns
        -------
        list
            One or more of: `problem`, `driver`, `<component hierarchy location>`,
            `<solver hierarchy location>`
        """
        pass

    def list_source_vars(self, source):
        """
        List of all the different recording sources for which there is recorded data.

        Parameters
        ----------
        source : {'problem', 'driver', iteration_coordinate}
            Identifies which cases to return. 'iteration_coordinate' can refer to
            a system or a solver hierarchy location. Defaults to 'problem'.

        Returns
        -------
        dict
            {'inputs':[list of keys], 'outputs':[list of keys]}. Does not recurse.
        """
        pass


class SqliteCaseReader(BaseCaseReader):
    """
    A CaseReader specific to files created with SqliteRecorder.

    Attributes
    ----------
    problem_metadata : dict
        Metadata about the problem, including the system hierachy and connections.
    system_metadata : dict
        Metadata about each system in the recorded model, including options and scaling factors.
    _format_version : int
        The version of the format assumed when loading the file.
    _solver_metadata : dict
        Metadata for all the solvers in the model, including their type and options
    _filename : str
        The path to the filename containing the recorded data.
    _solver_metadata :
    _abs2meta : dict
        Dictionary mapping variables to their metadata
    _abs2prom : {'input': dict, 'output': dict}
        Dictionary mapping absolute names to promoted names.
    _prom2abs : {'input': dict, 'output': dict}
        Dictionary mapping promoted names to absolute names.
    _output2meta : dict
        Dictionary mapping output variables to their metadata
    _input2meta : dict
        Dictionary mapping input variables to their metadata
    _driver_cases : DriverCases
        Helper object for accessing cases from the driver_iterations table.
    _deriv_cases : DerivCases
        Helper object for accessing cases from the driver_derivatives table.
    _system_cases : SystemCases
        Helper object for accessing cases from the system_iterations table.
    _solver_cases : SolverCases
        Helper object for accessing cases from the solver_iterations table.
    _problem_cases : ProblemCases
        Helper object for accessing cases from the problem_cases table.
    """

    def __init__(self, filename, pre_load=False):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The path to the filename containing the recorded data.
        pre_load : bool
            If True, load all the data into memory during initialization.
        """
        super(SqliteCaseReader, self).__init__(filename, pre_load)

        check_valid_sqlite3_db(filename)

        # initialize private attributes
        self._filename = filename
        self._solver_metadata = {}
        self._abs2prom = None
        self._prom2abs = None
        self._abs2meta = None
        self._output2meta = None
        self._input2meta = None

        # collect metadata from database
        with sqlite3.connect(filename) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            # collect data from the metadata table. this includes:
            #   format_version
            #   VOI metadata, which is added to problem_metadata
            #   var name maps and metadata for all vars, which are saved as private attributes
            self._collect_metadata(cur)

            # collect data from the driver_metadata table. this includes:
            #   model viewer data, which is added to problem_metadata
            self._collect_driver_metadata(cur)

            # collect data from the system_metadata table. this includes:
            #   component metadata and scaling factors for each system,
            #   which is added to system_metadata
            self._collect_system_metadata(cur)

            # collect data from the solver_metadata table. this includes:
            #   solver class and options for each solver, which is saved as a private attribute
            self._collect_solver_metadata(cur)

        con.close()

        # create maps to facilitate accessing variable metadata using absolute or promoted name
        self._output2meta = PromotedToAbsoluteMap(self._abs2meta, self._prom2abs, self._abs2prom, 1)
        self._input2meta = PromotedToAbsoluteMap(self._abs2meta, self._prom2abs, self._abs2prom, 0)

        # create helper objects for accessing cases from the four iteration tables and
        # the problem cases table
        voi_meta = self.problem_metadata['variables']
        self._driver_cases = DriverCases(filename, self._format_version,
                                         self._prom2abs, self._abs2prom, self._abs2meta, voi_meta)
        self._system_cases = SystemCases(filename, self._format_version,
                                         self._prom2abs, self._abs2prom, self._abs2meta, voi_meta)
        self._solver_cases = SolverCases(filename, self._format_version,
                                         self._prom2abs, self._abs2prom, self._abs2meta, voi_meta)
        if self._format_version >= 2:
            self._problem_cases = ProblemCases(filename, self._format_version,
                                               self._prom2abs, self._abs2prom, self._abs2meta, voi_meta)

        # if requested, load all the iteration data into memory
        if pre_load:
            self._load_cases()

    def _collect_metadata(self, cur):
        """
        Load data from the metadata table.

        Populates the `format_version` attribute and the `variables` data in
        the `problem_metadata` attribute of this CaseReader. Also saves the
        variable name maps and variable metadata to private attributes.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute('select * from metadata')

        row = cur.fetchone()

        # get format_version
        self._format_version = version = row['format_version']

        if version not in range(1, format_version + 1):
            raise ValueError('SQliteCaseReader encountered an unhandled '
                             'format version: {0}'.format(self._format_version))

        # add metadata for VOIs (des vars, objective, constraints) to problem metadata
        if version >= 4:
            self.problem_metadata['variables'] = json_loads(row['var_settings'])
        else:
            self.problem_metadata['variables'] = None

        # get variable name maps and metadata for all variables
        if version >= 3:
            self._abs2prom = json_loads(row['abs2prom'])
            self._prom2abs = json_loads(row['prom2abs'])
            self._abs2meta = json_loads(row['abs2meta'])

            # need to convert bounds to numpy arrays
            for name, meta in iteritems(self._abs2meta):
                if 'lower' in meta:
                    meta['lower'] = convert_to_np_array(meta['lower'], name, meta['shape'])
                if 'upper' in meta:
                    meta['upper'] = convert_to_np_array(meta['upper'], name, meta['shape'])

        elif version in (1, 2):
            abs2prom = row['abs2prom']
            prom2abs = row['prom2abs']
            abs2meta = row['abs2meta']

            if PY2:
                self._abs2prom = pickle.loads(str(abs2prom))
                self._prom2abs = pickle.loads(str(prom2abs))
                self._abs2meta = pickle.loads(str(abs2meta))

            if PY3:
                try:
                    self._abs2prom = pickle.loads(abs2prom)
                    self._prom2abs = pickle.loads(prom2abs)
                    self._abs2meta = pickle.loads(abs2meta)
                except TypeError:
                    # Reading in a python 2 pickle recorded pre-OpenMDAO 2.4.
                    self._abs2prom = pickle.loads(abs2prom.encode())
                    self._prom2abs = pickle.loads(prom2abs.encode())
                    self._abs2meta = pickle.loads(abs2meta.encode())

    def _collect_driver_metadata(self, cur):
        """
        Load data from the driver_metadata table.

        Populates the `problem_metadata` attribute of this CaseReader.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute("SELECT model_viewer_data FROM driver_metadata")
        row = cur.fetchone()

        if row is not None:
            if self._format_version >= 3:
                driver_metadata = json_loads(row[0])
            elif self._format_version in (1, 2):
                if PY2:
                    driver_metadata = pickle.loads(str(row[0]))
                if PY3:
                    driver_metadata = pickle.loads(row[0])

            self.problem_metadata.update(driver_metadata)

    def _collect_system_metadata(self, cur):
        """
        Load data from the system table.

        Populates the `system_metadata` attribute of this CaseReader.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute("SELECT id, scaling_factors, component_metadata FROM system_metadata")
        for row in cur:
            id = row[0]
            self.system_metadata[id] = {}

            if PY2:
                self.system_metadata[id]['scaling_factors'] = pickle.loads(str(row[1]))
                self.system_metadata[id]['component_options'] = pickle.loads(str(row[2]))
            if PY3:
                self.system_metadata[id]['scaling_factors'] = pickle.loads(row[1])
                self.system_metadata[id]['component_options'] = pickle.loads(row[2])

    def _collect_solver_metadata(self, cur):
        """
        Load data from the solver_metadata table.

        Populates the `solver_metadata` attribute of this CaseReader.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute("SELECT id, solver_options, solver_class FROM solver_metadata")
        for row in cur:
            id = row[0]
            if PY2:
                solver_options = pickle.loads(str(row[1]))
            if PY3:
                solver_options = pickle.loads(row[1])
            solver_class = row[2]
            self._solver_metadata[id] = {
                'solver_options': solver_options,
                'solver_class': solver_class,
            }

    def _load_cases(self):
        """
        Load all driver, solver, and system cases into memory.
        """
        self._driver_cases._load_cases()
        self._solver_cases._load_cases()
        self._system_cases._load_cases()
        if self._format_version >= 2:
            self._problem_cases._load_cases()

    def list_sources(self):
        """
        List of all the different recording sources for which there is recorded data.

        Returns
        -------
        list
            One or more of: `problem`, `driver`, `<component hierarchy location>`,
            `<solver hierarchy location>`
        """
        sources = []

        if self._driver_cases.count() > 0:
            sources.extend(self._driver_cases.list_sources())
        if self._solver_cases.count() > 0:
            sources.extend(self._solver_cases.list_sources())
        if self._system_cases.count() > 0:
            sources.extend(self._system_cases.list_sources())
        if self._format_version >= 2 and self._problem_cases.count() > 0:
            sources.extend(self._problem_cases.list_sources())

        return sources

    def list_cases(self, source='driver', recurse=False, flat=False):
        """
        Iterate over the driver and solver cases.

        Generator giving Driver and/or Solver cases in order.

        Parameters
        ----------
        source : {'problem', 'driver', component pathname, solver pathname}
            Identifies which cases to return.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy.
        flat : bool, optional
            If False and there are child cases, then a nested ordered dictionary
            is returned rather than an iterator.

        Returns
        -------
        iterator or dict
            An iterator or a nested dictionary of identified cases.
        """
        if not isinstance(source, str):
            raise TypeError("'source' parameter must be a string.")

        elif source == 'driver':
            if not recurse:
                return self._driver_cases.list_cases()
            elif flat:
                cases = []
                solver_cases = self._solver_cases.list_cases()
                system_cases = self._system_cases.list_cases()
                driver_cases = self._driver_cases.list_cases()
                for driver_coord in driver_cases:
                    for solver_coord in solver_cases:
                        if solver_coord.startswith(driver_coord):
                            cases.append(solver_coord)
                    for system_coord in system_cases:
                        if system_coord.startswith(driver_coord):
                            cases.append(system_coord)
                    cases.append(driver_coord)
                return cases
            else:
                # return nested dicts of cases and child cases
                cases = OrderedDict()
                driver_cases = self._driver_cases.list_cases()
                for driver_coord in driver_cases:
                    driver_case = cases[driver_coord] = OrderedDict()
                    solver_cases = self._solver_cases.list_cases(driver_coord, recurse=True)
                    for solver_case in solver_cases:
                        driver_case[solver_case] = solver_cases[solver_case]
                    system_cases = self._system_cases.list_cases(driver_coord, recurse=True)
                    for system_case in system_cases:
                        driver_case[system_case] = system_cases[system_case]
                return cases

        elif source == 'problem':
            if self._format_version >= 2:
                return self._problem_cases.list_cases(recurse, flat)
            else:
                raise RuntimeError('No problem cases recorded (data format = %d).' %
                                   self._format_version)

        elif source in self._system_cases.list_sources():
            return self._system_cases.list_cases(source, recurse, flat)

        elif source in self._solver_cases.list_sources():
            return self._solver_cases.list_cases(source, recurse, flat)

        else:
            raise RuntimeError('Source not found: %s.' % source)

    def get_cases(self, source='driver', recurse=False, flat=False):
        """
        Iterate over the driver and solver cases.

        Generator giving Driver and/or Solver cases in order.

        Parameters
        ----------
        source : {'problem', 'driver', component pathname, solver pathname, iteration_coordinate}
            Identifies which cases to return.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
        flat : bool, optional
            If False and there are child cases, then a nested ordered dictionary
            is returned rather than an iterator.

        Returns
        -------
        iterable or dict
            The cases identified by source
        """
        if not isinstance(source, str):
            raise TypeError("'source' parameter must be a string.")

        elif source == 'driver':
            if not recurse:
                # return driver cases only
                for case in self._driver_cases.cases():
                    yield case
            elif flat:
                # return driver cases and child cases
                cases = self.list_cases('driver', recurse, flat)
                for case_id in cases:
                    yield self.get_case(case_id)
            else:
                # return nested dicts of cases and child cases
                cases = OrderedDict()
                driver_cases = self._driver_cases.list_cases()
                for driver_coord in driver_cases:
                    driver_case = cases[driver_coord] = OrderedDict()
                    solver_cases = self._solver_cases.list_cases(driver_coord, recurse=True)
                    for solver_case in solver_cases:
                        driver_case[solver_case] = self.get_case(solver_cases[solver_case])
                    system_cases = self._system_cases.list_cases(driver_coord, recurse=True)
                    for system_case in system_cases:
                        driver_case[system_case] = self.get_case(system_cases[system_case])
                return cases

        elif source == 'problem':
            if self._format_version >= 2:
                for case in self._problem_cases.cases():
                    yield case
            else:
                raise RuntimeError('No problem cases recorded (data format = %d).' %
                                   self._format_version)

        elif source in self._system_cases.list_sources():
            for case_id in self._system_cases.list_cases(source, recurse, flat):
                yield self._system_cases.get_case(case_id)

        elif source in self._solver_cases.list_sources():
            for case_id in self._solver_cases.list_cases(source, recurse, flat):
                yield self._solver_cases.get_case(case_id)

        else:
            # source is an iteration coordinate
            iter_coord = source

            driver_cases = self._driver_cases.list_cases(iter_coord)
            if driver_cases:
                if flat:
                    for driver_coord in driver_cases:
                        # first the child solver cases
                        for case in self._solver_cases.get_cases(driver_coord, recurse, flat):
                            yield case

                        # then the child system cases
                        for case in self._system_cases.get_cases(driver_coord, recurse, flat):
                            yield case

                        # and finally the driver case itself
                        yield self._driver_cases.get_case(driver_coord)
                else:
                    # return nested dicts of cases and child cases
                    raise RuntimeError('TODO: implement nested dicts')
            else:
                system_cases = self._system_cases.list_cases(iter_coord)
                if flat:
                    for system_coord in system_cases:
                        # first the child solver cases
                        for case in self._solver_cases.get_cases(system_coord, recurse, flat):
                            yield case

                        # then the system cases
                        for case in self._system_cases.get_cases(system_coord, recurse, flat):
                            yield case
                else:
                    # return nested dicts of cases and child cases
                    raise RuntimeError('TODO: implement nested dicts')

    def get_case(self, case_id, recurse=False):
        """
        Initialize.

        Parameters
        ----------
        case_id : str
            The unique identifier of the case to return.
        recurse : bool, optional
            If True, will return all successors to the case as well.

        Returns
        -------
        dict
            The case identified by case_id
        """
        tables = [self._driver_cases, self._system_cases, self._solver_cases]
        if self._format_version >= 2:
            tables.append(self._problem_cases)

        for table in tables:
            case = table.get_case(case_id)
            if case and not recurse:
                return case

        if case and recurse:
            raise RuntimeError('TODO: return nested case:', case_id)
        else:
            raise RuntimeError('Case not found:', case_id)


class CaseTable(object):
    """
    Base class for wrapping case tables in a recording database.

    Attributes
    ----------
    _filename : str
        The name of the recording file from which to instantiate the case reader.
    _format_version : int
        The version of the format assumed when loading the file.
    _table_name : str
        The name of the table in the database.
    _index_name : str
        The name of the case index column in the table.
    _abs2prom : {'input': dict, 'output': dict}
        Dictionary mapping absolute names to promoted names.
    _abs2meta : dict
        Dictionary mapping absolute variable names to variable metadata.
    _prom2abs : {'input': dict, 'output': dict}
        Dictionary mapping promoted names to absolute names.
    _voi_meta : dict
        Dictionary mapping absolute variable names to variable settings.
    _keys : list
        List of keys of cases in the table.
    _cases : dict
        Dictionary mapping keys to cases that have already been loaded.
    """

    def __init__(self, filename, format_version, table, index, prom2abs, abs2prom, abs2meta, voi_meta=None):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        table : str
            The name of the table in the database.
        index : str
            The name of the case index column in the table.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        voi_meta : dict
            Dictionary mapping absolute variable names to variable settings (relevant to driver cases).
        """
        self._filename = filename
        self._format_version = format_version
        self._table_name = table
        self._index_name = index
        self._prom2abs = prom2abs
        self._abs2prom = abs2prom
        self._abs2meta = abs2meta
        self._voi_meta = voi_meta

        # cached keys/cases
        self._keys = []
        self._cases = {}

    def count(self):
        """
        Get the number of cases recorded in the table.

        Returns
        -------
        int
            The number of cases recorded in the table.
        """
        with sqlite3.connect(self._filename) as con:
            cur = con.cursor()
            cur.execute("SELECT count(*) FROM %s" % self._table_name)
            rows = cur.fetchall()

        con.close()

        return rows[0][0]

    def list_cases(self, source=None, recurse=False, flat=False):
        """
        Get list of case names for cases in the table.

        Parameters
        ----------
        source : str, optional
            If not None, only cases that have the specified source will be returned
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
        flat : bool, optional
            If False and there are child cases, then a nested ordered dictionary
            is returned rather than an iterator.

        Returns
        -------
        list or dict
            The cases from the table that have the specified source.
        """
        with sqlite3.connect(self._filename) as con:
            cur = con.cursor()
            cur.execute("SELECT %s FROM %s ORDER BY id ASC" %
                        (self._index_name, self._table_name))
            rows = cur.fetchall()

        con.close()

        if not source:
            # return all cases
            return [row[0] for row in rows]
        elif '|' in source:
            # source is a coordinate
            if recurse and not flat:
                cases = OrderedDict()
                for row in rows:
                    if len(row[0]) > len(source) and row[0].startswith(source):
                        cases[row[0]] = self.list_cases(row[0], recurse, flat)
                return cases
            else:
                return [row[0] for row in rows if row[0].startswith(source)]
        else:
            # source is a system or solver
            if recurse:
                if flat:
                    # return all cases under the source system
                    source_sys = source.replace('.nonlinear_solver', '')
                    return [row[0] for row in rows
                            if self._get_source(row[0]).startswith(source_sys)]
                else:
                    cases = OrderedDict()
                    for row in rows:
                        row_source = self._get_source(row[0])
                        if row_source == source:
                            cases[row[0]] = self.list_cases(row[0], recurse, flat)
                    return cases
            else:
                return [row[0] for row in rows
                        if self._get_source(row[0]) == source]

    def get_cases(self, source=None, recurse=False, flat=False):
        """
        Get list of case names for cases in the table.

        Parameters
        ----------
        source : str, optional
            If not None, only cases that have the specified source will be returned
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
        flat : bool, optional
            If False and there are child cases, then a nested ordered dictionary
            is returned rather than an iterator.

        Returns
        -------
        list or dict
            The cases from the table that have the specified source.
        """
        with sqlite3.connect(self._filename) as con:
            cur = con.cursor()
            cur.execute("SELECT %s FROM %s ORDER BY id ASC" %
                        (self._index_name, self._table_name))
            rows = cur.fetchall()

        con.close()

        if not source:
            # return all cases
            return [self.get_case(row[0]) for row in rows]
        elif '|' in source:
            # source is a coordinate
            if recurse and not flat:
                cases = OrderedDict()
                for row in rows:
                    if len(row[0]) > len(source) and row[0].startswith(source):
                        cases[row[0]] = self.get_cases(row[0], recurse, flat)
                return cases
            else:
                return [self.get_case(row[0]) for row in rows if row[0].startswith(source)]
        else:
            # source is a system or solver
            if recurse:
                if flat:
                    # return all cases under the source system
                    source_sys = source.replace('.nonlinear_solver', '')
                    return [self.get_case(row[0]) for row in rows
                            if self._get_source(row[0]).startswith(source_sys)]
                else:
                    cases = OrderedDict()
                    for row in rows:
                        row_source = self._get_source(row[0])
                        if row_source == source:
                            cases[row[0]] = self.get_cases(row[0], recurse, flat)
                    return cases
            else:
                return [self.get_case(row[0]) for row in rows
                        if self._get_source(row[0]) == source]

    def get_case(self, case_id, cache=False):
        """
        Get a case from the database.

        Parameters
        ----------
        case_id : str or int
            The string-identifier of the case to be retrieved or the index of the case.
        cache : bool
            If True, case will be cached for faster access by key.

        Returns
        -------
        Case
            The specified case from the table.
        """
        # If case_id is an integer, then it is an index into the keys
        if isinstance(case_id, int):
            case_id = self._get_iteration_coordinate(case_id)

        # if we've already cached this case, return the cached instance
        if case_id in self._cases:
            return self._cases[case_id]

        # we don't have it, so fetch it
        with sqlite3.connect(self._filename) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT * FROM %s WHERE %s='%s'" %
                        (self._table_name, self._index_name, case_id))
            row = cur.fetchone()

        con.close()

        # if found, extract the data and optionally cache the Case
        if row is not None:
            source = self._get_source(row[self._index_name])
            case = Case(source, row,
                        self._prom2abs, self._abs2prom, self._abs2meta, self._voi_meta,
                        self._format_version)

            # cache it if requested
            if cache:
                self._cases[case_id] = case

            return case
        else:
            return None

    def _get_iteration_coordinate(self, case_idx):
        """
        Return the iteration coordinate for the indexed case (handles negative indices, etc.).

        Parameters
        ----------
        case_idx : int
            The case number that we want the iteration coordinate for.

        Returns
        -------
        iteration_coordinate : str
            The iteration coordinate.
        """
        # if keys have not been cached yet, get them now
        if not self._keys:
            self._keys = self.list_cases(recurse=True, flat=True)

        return self._keys[case_idx]

    def cases(self, cache=False):
        """
        Iterate over all cases, optionally caching them into memory.

        Parameters
        ----------
        cache : bool
            If True, cases will be cached for faster access by key.
        """
        with sqlite3.connect(self._filename) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT * FROM %s ORDER BY id ASC" % self._table_name)
            # rows = cur.fetchall()
            for row in cur:
                case_id = row[self._index_name]
                source = self._get_source(case_id)
                # print('cases()', source)
                # pprint(dict(zip(row.keys(), row)))
                case = Case(source, row,
                            self._prom2abs, self._abs2prom, self._abs2meta, self._voi_meta,
                            self._format_version)
                if cache:
                    self._cases[case_id] = case
                yield case

        con.close()

    def _load_cases(self):
        """
        Load all cases into memory.
        """
        for case in self.cases(cache=True):
            pass

    def list_sources(self):
        """
        Get the list of sources that recorded data in this table.

        Returns
        -------
        list
            List of sources.
        """
        return set([self._get_source(case) for case in self.list_cases()])

    def _get_source(self, iteration_coordinate):
        """
        Get pathname of system that is the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            The pathname of the system that is the source of the iteration.
        """
        path = []
        parts = _coord_split_re.split(iteration_coordinate)
        for part in parts:
            if (_coord_system_re.search(part) is not None):
                if ':' in part:
                    # get rid of 'rank#:'
                    part = part.split(':')[1]
                path.append(part.split('.')[0])

        # return pathname of the system
        return '.'.join(path)

    def _get_first(self, source):
        """
        Get the first case from the specified source.

        Parameters
        ----------
        source : str
            The source.

        Returns
        -------
        Case
            The first case from the specified source.
        """
        for case in self.cases():
            if case.source == source:
                return case

        return None


class DriverCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a Driver iteration.

    Attributes
    ----------
    _voi_meta : dict
        Dictionary mapping absolute variable names to variable settings.
    """

    def __init__(self, filename, format_version, prom2abs, abs2prom, abs2meta, voi_meta):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        voi_meta : dict
            Dictionary mapping absolute variable names to variable settings.
        """
        super(DriverCases, self).__init__(filename, format_version,
                                          'driver_iterations', 'iteration_coordinate',
                                          prom2abs, abs2prom, abs2meta, voi_meta)
        self._voi_meta = voi_meta

    def cases(self, cache=False):
        """
        Iterate over all cases, optionally caching them into memory.

        Override base class to add derivatives from the derivatives table.

        Parameters
        ----------
        cache : bool
            If True, cases will be cached for faster access by key.
        """
        with sqlite3.connect(self._filename) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT * FROM %s ORDER BY id ASC" % self._table_name)
            rows = cur.fetchall()

            for row in rows:
                if self._format_version > 1:
                    # fetch associated derivative data, if available
                    cur.execute("SELECT * FROM driver_derivatives WHERE iteration_coordinate='%s'"
                                % row['iteration_coordinate'])
                    derivs_row = cur.fetchone()

                    if derivs_row:
                        # convert row to a regular dict and add jacobian
                        row = dict(zip(row.keys(), row))
                        row['jacobian'] =  derivs_row['derivatives']

                case = Case('driver', row,
                            self._prom2abs, self._abs2prom, self._abs2meta, self._voi_meta,
                            self._format_version)

                if cache:
                    self._cases[case.iteration_coordinate] = case

                yield case

        con.close()

    def get_case(self, case_id, cache=False):
        """
        Get a case from the database.

        Parameters
        ----------
        case_id : int or str
            The integer index or string-identifier of the case to be retrieved.
        cache : bool
            If True, cache the case so it does not have to be fetched on next access.

        Returns
        -------
        Case
            The specified case from the driver_iterations and driver_derivatives tables.
        """
        # check to see if we've already cached this case
        if isinstance(case_id, int):
            case_id = self._get_iteration_coordinate(case_id)

        # return cached case if present, else fetch it
        if case_id in self._cases:
            return self._cases[case_id]

        # Get an unscaled case if does not already exist in _cases
        with sqlite3.connect(self._filename) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            # fetch driver iteration data
            cur.execute("SELECT * FROM driver_iterations WHERE "
                        "iteration_coordinate=:iteration_coordinate",
                        {"iteration_coordinate": case_id})
            row = cur.fetchone()

            # fetch associated derivative data, if available
            if row and self._format_version > 1:
                cur.execute("SELECT * FROM driver_derivatives WHERE "
                            "iteration_coordinate=:iteration_coordinate",
                            {"iteration_coordinate": case_id})
                derivs_row = cur.fetchone()

                if derivs_row:
                    # convert row to a regular dict and add jacobian
                    row = dict(zip(row.keys(), row))
                    row['jacobian'] =  derivs_row['derivatives']
        con.close()

        # if found, create Case object (and cache it if requested) else return None
        if row:
            case = Case('driver', row,
                        self._prom2abs, self._abs2prom, self._abs2meta, self._voi_meta,
                        self._format_version)
            if cache:
                self._cases[case_id] = case
            return case
        else:
            return None

    def list_sources(self):
        """
        Get the list of sources that recorded data in this table (just the driver).

        Returns
        -------
        list
            List of sources.
        """
        return ['driver']

    def _get_source(self, iteration_coordinate):
        """
        Get the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            'driver' (all cases in this table come from the driver).
        """
        return 'driver'


class ProblemCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a Driver iteration.
    """

    def __init__(self, filename, format_version, prom2abs, abs2prom, abs2meta, voi_meta):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        """
        super(ProblemCases, self).__init__(filename, format_version,
                                           'problem_cases', 'case_name',
                                           prom2abs, abs2prom, abs2meta, voi_meta)

    def list_sources(self):
        """
        Get the list of sources that recorded data in this table (just the problem).

        Returns
        -------
        list
            List of sources.
        """
        return ['problem']

    def _get_source(self, iteration_coordinate):
        """
        Get the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            'problem' (all cases in this table come from the problem).
        """
        return 'problem'


class SystemCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a System iteration.
    """

    def __init__(self, filename, format_version, prom2abs, abs2prom, abs2meta, voi_meta):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        """
        super(SystemCases, self).__init__(filename, format_version,
                                          'system_iterations', 'iteration_coordinate',
                                          prom2abs, abs2prom, abs2meta, voi_meta)


class SolverCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a Solver iteration.
    """

    def __init__(self, filename, format_version, prom2abs, abs2prom, abs2meta, voi_meta):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        """
        super(SolverCases, self).__init__(filename, format_version,
                                          'solver_iterations', 'iteration_coordinate',
                                          prom2abs, abs2prom, abs2meta, voi_meta)

    def _get_source(self, iteration_coordinate):
        """
        Get pathname of solver that is the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            The pathname of the solver that is the source of the iteration.
        """
        return super(SolverCases, self)._get_source(iteration_coordinate) + '.nonlinear_solver'
