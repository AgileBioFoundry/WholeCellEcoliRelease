from __future__ import division
from __future__ import print_function

import os
import json
import warnings

import numpy as np
import pandas as pd

import tablewriter as tw

ZIP_FILETYPE = ".bz2"

__all__ = [
        "TableReader",
        ]

class TableReaderError(Exception):
        """
        Base exception class for TableReader-associated exceptions.
        """
        pass


class NotUnzippedError(TableReaderError):
        """
        An error raised when it appears that the input files are compressed.
        """
        pass


class VersionError(TableReaderError):
        """
        An error raised when the input files claim to be from a different version
        of the file specification.
        """
        pass


class DoesNotExistError(TableReaderError):
        """
        An error raised when a column or attribute does not seem to exist.
        """
        pass


class VariableWidthError(TableReaderError):
        """
        An error raised when trying to load an entire column as one array when
        entry sizes vary.
        """
        pass


class TableReader(object):
        """
        Reads output generated by TableWriter.

        Parameters
        ----------
        path : str
                Path to the input location (a directory).

        See also
        --------
        wholecell.io.tablewriter.TableWriter

        Notes
        -----
        TODO (John): Consider a method for loading an indexed portion of a column.

        TODO (John): Unit tests.

        """

        def __init__(self, path):
                # Open version file for table
                versionFilePath = os.path.join(path, tw.DIR_METADATA, tw.FILE_VERSION)
                try:
                        with open(versionFilePath) as f:
                                version = f.read()

                except IOError as e:
                        # Check if a zipped version file exists. Print appropriate error prompts.
                        if os.path.exists(versionFilePath + ZIP_FILETYPE):
                                raise NotUnzippedError("The version file for a table ({}) was found zipped. Unzip all table files before reading table.".format(path), e)
                        else:
                                raise VersionError("Could not open the version file for a table ({})".format(path), e)

                # Check if the table version matches the latest version
                if version != tw.VERSION:
                        raise VersionError("Expected version {} but found version {}".format(tw.VERSION, version))

                # Read attribute names for table
                self._dirAttributes = os.path.join(path, tw.DIR_ATTRIBUTES)
                self._attributeNames = os.listdir(self._dirAttributes)

                # Read column names for table
                self._dirColumns = os.path.join(path, tw.DIR_COLUMNS)
                self._columnNames = os.listdir(self._dirColumns)


        def readAttribute(self, name):
                """
                Load an attribute.

                Parameters
                ----------
                name : str
                        The name of the attribute.

                Returns
                -------
                The attribute, JSON-deserialized from a string.

                """

                if name not in self._attributeNames:
                        raise DoesNotExistError("No such attribute: {}".format(name))

                with open(os.path.join(self._dirAttributes, name)) as f:
                    return json.load(f)
                        


        def readColumn(self, name, indices=None, block_read=True):
                """
                Load a full column (all entries).

                Parameters:
                        name (str): The name of the column.
                        indices (ndarray): Numpy array of ints. The specific indices at each
                                time point to read. If None, reads in all data. If provided, can
                                give a performance boost for files with many entries.
                                NOTE: performance benefit might only be realized if the file
                                is in the disk cache (i.e. the file has been recently read),
                                which should typically be the case.
                        block_read (bool): If True, will only read one block per time point,
                                otherwise will seek between contiguous data. Only applies if
                                indices are given.
                                NOTE: If False and indices are spread out, reading can be orders
                                of magnitude slower.

                Returns:
                        ndarray: data read with entries along the first dimension

                Notes:
                If entry sizes varies, this method cannot be used.

                Output will be squeezed; e.g. scalars or scalar-likes written with
                TableWriter will be returned as vectors.

                TODO (John): Consider using np.memmap to defer loading of the data
                        until it is operated on.  Early work (see issue #221) suggests that
                        this may lead to cryptic performance issues.

                TODO: Select criteria to automatically select between two methods for indices
                """

                if name not in self._columnNames:
                        raise DoesNotExistError("No such column: {}".format(name))

                offsets, dtype = self._loadOffsets(name)

                sizes = np.diff(offsets)

                if len(set(sizes)) > 1:
                        raise VariableWidthError("Cannot load full column; data size varies")

                nEntries = sizes.size

                with open(os.path.join(self._dirColumns, name, tw.FILE_DATA), 'rb') as dataFile:
                        if indices is None:
                                dataFile.seek(offsets[0])

                                return np.frombuffer(
                                        dataFile.read(), dtype
                                        ).reshape(nEntries, -1).squeeze()
                        else:
                                type_size = np.dtype(dtype).itemsize
                                n_items = int(sizes[0] / type_size)
                                data = np.zeros((nEntries, len(indices)), dtype)

                                dataFile.seek(offsets[0])

                                # Precalculate seeks for each entry
                                # Sort to improve speed of seeking
                                sort_indices = np.argsort(indices)
                                indices = indices[sort_indices]
                                seeks = np.zeros_like(indices)
                                seeks[0] = indices[0] * type_size
                                seeks[1:] = (indices[1:] - indices[:-1] - 1) * type_size
                                last_seek = (n_items - indices[-1] - 1) * type_size

                                if block_read:
                                        # Read only from first to last index of interest
                                        seek = last_seek + seeks[0]
                                        indices -= indices[0]
                                        dataFile.seek(seeks[0], 1)
                                        for i in range(nEntries):
                                                data[i, sort_indices] = np.frombuffer(
                                                        dataFile.read((indices[-1]+1) * type_size), dtype
                                                        )[indices]
                                                dataFile.seek(seek, 1)
                                else:
                                        # Group contiguous data (seek of 0) into one read
                                        grouped_indices = []
                                        read_lengths = []
                                        new_seeks = [seeks[0]]
                                        current_group = [sort_indices[0]]
                                        for idx, seek in zip(sort_indices[1:], seeks[1:]):
                                                if seek == 0:
                                                        current_group += [idx]
                                                else:
                                                        new_seeks += [seek]
                                                        read_lengths += [len(current_group)]
                                                        grouped_indices += [np.array(current_group)]
                                                        current_group = [idx]
                                        read_lengths += [len(current_group)]
                                        grouped_indices += [np.array(current_group)]

                                        # Loop over data to read only indices of interest
                                        read_info = zip(grouped_indices, read_lengths, new_seeks)
                                        for i in range(nEntries):
                                                for idx, n_reads, seek in read_info:
                                                        dataFile.seek(seek, 1)
                                                        data[i, idx] = np.frombuffer(
                                                                dataFile.read(n_reads * type_size), dtype
                                                                )
                                                dataFile.seek(last_seek, 1)

                                return data.squeeze()


        def _loadEntry(self, name, index):
                """
                Internal method for loading a column value for a given entry.

                Parameters
                ----------
                name : str
                        Name of the column.
                index : int
                        Index of the entry.

                Returns
                -------
                NumPy ndarray.

                """

                offsets, dtype = self._loadOffsets(name)

                size = offsets[index+1] - offsets[index]

                with open(os.path.join(self._dirColumns, name, tw.FILE_DATA), 'rb') as dataFile:
                        dataFile.seek(offsets[index])

                        return np.frombuffer(
                                dataFile.read(size), dtype
                                )


        def _loadOffsets(self, name):
                """
                Internal method for loading data needed to interpret a column.

                Parameters
                ----------
                name : str
                        The name of the column.

                Returns
                -------
                offsets : ndarray (int)
                        An integer array.  Each element corresponds to the offset (in
                        bytes) for each entry in the column's data file.
                dtype : list-of-tuples-of-strings
                        A data type specific for instantiating a NumPy ndarray.

                """

                with open(os.path.join(self._dirColumns, name, tw.FILE_OFFSETS), 'rb') as offsetsFile:
                        offsets = np.array([int(i.strip()) for i in offsetsFile])

                with open(os.path.join(self._dirColumns, name, tw.FILE_DATA), 'rb') as dataFile:
                        rawDtype = json.loads(dataFile.read(offsets[0]))

                        if rawDtype[0] == "<":
                                dtype = str(rawDtype)
                        else:
                                dtype = [ # numpy requires list-of-tuples-of-strings
                                        (str(n), str(t))
                                        for n, t in rawDtype
                                        ]

                return offsets, dtype


        def attributeNames(self):
                """
                Returns the names of all attributes.
                """
                return self._attributeNames


        def columnNames(self):
                """
                Returns the names of all columns.
                """
                return self._columnNames


        def as_pandas(self, column, attribute=None):
            """Load a specified 'column' into a dataframe;
            essentially a fancy wrapper for for readColumn.

            Automatically matches to attribute of the correct shape IF
            there is only one such attribute OR if all attributes with the
            correct shape have the same values.

            If there are no attributes, just returns the data as a dataframe.

            column -- Which column wrapped by this reader to load into a dataframe.
                       The valid 'column's are provide by the 'columnNames' attribute.

            attribute -- Which attribue to provide labels in the result columns of
                       the resulting dataframe.  Attributes are automatched based on
                       shape if it is not ambiguous.  Valid attributes are
                       'attributeNames' attribute.
            """
            

            def _shape_match(data, axis=1):
                """Which attributes have the right shape for the data passed?
                data -- a numpy array (or similar)
                axis -- Which data dimension to match on (default is 1)
                """

                target_size = data.shape[axis]
                candidates = [att for att, size in att_shapes.items()
                              if size == target_size]
                return candidates

            def _are_attributes_equal(attribute_names):
                "Given a list of attribute names, are their values all equal?"
                attribute_values = [self.readAttribute(n) for n in attribute_names]
                pairs = zip(attribute_values[:-1], attribute_values[1:])
                equalities = [a==b for a,b in pairs]
                return all(equalities)

            data = self.readColumn(column)
            att_shapes = {name: len(self.readAttribute(name)) for name in self.attributeNames()}

            if attribute is None and len(att_shapes) > 0 and len(data.shape) > 1:
                matches = _shape_match(data)
                if len(matches) > 1 and not _are_attributes_equal(matches):
                    raise ValueError(f"Attribute matching ambiguous for {column}, specify 'attribute'. "\
                                     f"Options found: {', '.join(matches)}.")
                elif len(matches) == 0:
                    warnings.warn(f"No attributes match shape for {column}.  Returning data without labels.")
                else:
                    attribute = matches[0]

            labels = self.readAttribute(attribute) if attribute is not None else None
            return pd.DataFrame(data, columns=labels)
