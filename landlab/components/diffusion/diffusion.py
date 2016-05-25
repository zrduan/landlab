#! /usr/env/python
"""

Component that models 2D diffusion using an explicit finite-volume method.

Created July 2013 GT
Last updated March 2016 DEJH with LL v1.0 component style
"""

from __future__ import print_function

import numpy as np
from six.moves import range

from landlab import ModelParameterDictionary, Component, FieldError, \
    create_and_initialize_grid, FIXED_GRADIENT_BOUNDARY, FIXED_LINK
from landlab.core.model_parameter_dictionary import MissingKeyError
from landlab.utils.decorators import use_file_name_or_kwds

_ALPHA = 0.15   # time-step stability factor
# ^0.25 not restrictive enough at meter scales w S~1 (possible cases)


class LinearDiffuser(Component):
    """
    This component implements linear diffusion of a Landlab field.

    Component assumes grid does not deform. If the boundary conditions on the
    grid change after component instantiation, be sure to also call
    :func:`updated_boundary_conditions` to ensure these are reflected in the
    component (especially if fixed_links are present).

    The primary method of this class is :func:`run_one_step`.

    Construction::

        LinearDiffuser(grid, linear_diffusivity=None, method='simple')

    Parameters
    ----------
    grid : ModelGrid
        A grid.
    linear_diffusivity : float, array, len-2 list, or field name (m**2/time)
        The diffusivity. If a len-2 tuple, must be contain 2 nnodes-long arrays
        giving diffusivity vector components at each node, [x_diff, y_diff].
    method : {'simple', 'resolve_on_patches'}
        The method used to represent the fluxes. 'simple' solves a finite
        difference method with a simple staggered grid scheme onto the links.
        'resolve_on_patches' solves the scheme by mapping both slopes and
        diffusivities onto the patches and solving there before resolving
        values back to the nodes. This latter technique is more computationally
        expensive, but can suppress cardinal direction artifacts if diffusion
        is performed on a raster.

    Examples
    --------
    >>> from landlab import RasterModelGrid
    >>> import numpy as np
    >>> mg = RasterModelGrid((9, 9), 1.)
    >>> z = mg.add_zeros('node', 'topographic__elevation')
    >>> z.reshape((9, 9))[4, 4] = 1.
    >>> mg.set_closed_boundaries_at_grid_edges(True, True, True, True)
    >>> ld = LinearDiffuser(mg, linear_diffusivity=1.)
    >>> for i in range(1):
    ...     ld.run_one_step(1.)
    >>> np.isclose(z[mg.core_nodes].sum(), 1.)
    True
    >>> mg2 = RasterModelGrid((5, 30), 1.)
    >>> z2 = mg2.add_zeros('node', 'topographic__elevation')
    >>> z2.reshape((5, 30))[2, 8] = 1.
    >>> z2.reshape((5, 30))[2, 22] = 1.
    >>> mg2.set_closed_boundaries_at_grid_edges(True, True, True, True)
    >>> kd = mg2.node_x/mg2.node_x.mean()
    >>> ld2 = LinearDiffuser(mg2, linear_diffusivity=kd)
    >>> for i in range(10):
    ...     ld2.run_one_step(0.1)
    >>> z2[mg2.core_nodes].sum() == 2.
    True
    >>> z2.reshape((5, 30))[2, 8] > z2.reshape((5, 30))[2, 22]
    True
    """

    _name = 'LinearDiffuser'

    _input_var_names = ('topographic__elevation',)

    _output_var_names = ('topographic__elevation',
                         'topographic__gradient',
                         'unit_flux',
                         )

    _var_units = {'topographic__elevation': 'm',
                  'topographic__gradient': '-',
                  'unit_flux': 'm**3/s',
                  }

    _var_mapping = {'topographic__elevation': 'node',
                    'topographic__gradient': 'link',
                    'unit_flux': 'link',
                    }

    _var_doc = {
        'topographic__elevation': ('Land surface topographic elevation; can ' +
                                   'be overwritten in initialization'),
        'topographic__gradient': 'Gradient of surface, on links',
        'unit_flux': 'Volume flux per unit width along links'
    }

    @use_file_name_or_kwds
    def __init__(self, grid, linear_diffusivity=None, method='simple',
                 **kwds):
        self._grid = grid
        assert method in ('simple', 'resolve_on_patches')
        if method == 'simple':
            self._use_patches = False
        else:
            self._use_patches = True
        self.current_time = 0.
        if linear_diffusivity is not None:
            if type(linear_diffusivity) is not str:
                if len(linear_diffusivity) is not 2:
                    self._kd = linear_diffusivity
                    if type(self._kd) in (float, int):
                        self._use_patch_kd = False
                        if type(self._kd) is int:
                            self._kd = float(self._kd)
                    else:
                        if self._kd.size == self.grid.number_of_nodes:
                            self._use_patch_kd = False
                        elif self._kd.size == self.grid.number_of_links:
                            self._use_patch_kd = True
                else:
                    assert linear_diffusivity[
                        0].size == self.grid.number_of_nodes
                    assert linear_diffusivity[
                        1].size == self.grid.number_of_nodes
                    self._kd = list(linear_diffusivity)
                    self._use_patch_kd = True
            else:
                try:
                    self._kd = self.grid.at_link[linear_diffusivity]
                    self._use_patch_kd = True
                except KeyError:
                    self._kd = self.grid.at_node[linear_diffusivity]
                    self._use_patch_kd = False
        else:
            raise KeyError("linear_diffusivity must be provided to the " +
                           "LinearDiffuser component")

        # for component back compatibility (undocumented):
        # note component can NO LONGER do internal uplift, at all.
        # ###
        self.timestep_in = kwds.pop('dt', None)
        if 'values_to_diffuse' in kwds.keys():
            self.values_to_diffuse = kwds.pop('values_to_diffuse')
            for mytups in (self._input_var_names, self._output_var_names):
                myset = set(mytups)
                myset.remove('topographic__elevation')
                myset.add(self.values_to_diffuse)
                mytups = tuple(myset)
            for mydicts in (self._var_units, self._var_mapping, self._var_doc):
                mydicts[self.values_to_diffuse] = mydicts.pop(
                    'topographic__elevation')
        else:
            self.values_to_diffuse = 'topographic__elevation'
        # Raise an error if somehow someone is using this weird functionality
        if self._grid is None:
            raise ValueError('You must now provide an existing grid!')
        # ###

        # Set internal time step
        # ..todo:
        #   implement mechanism to compute time-steps dynamically if grid is
        #   adaptive/changing
        # as of modern componentization (Spring '16), this can take arrays
        # and irregular grids
        if type(self._kd) not in (float, list):
            if self._kd.size == self.grid.number_of_nodes:
                kd_links = self.grid.map_max_of_link_nodes_to_link(self._kd)
            else:  # explicit length check already applied above
                kd_links = self._kd
        else:
            if type(self._kd) is float:
                kd_links = float(self._kd)
            else:
                kd_links = self.grid.map_max_of_link_nodes_to_link(
                    np.sqrt(self._kd[0]**2 + self._kd[1]**2))
        # assert CFL condition:
        CFL_prefactor = _ALPHA * self.grid.length_of_link[
            :self.grid.number_of_links] ** 2.
        # ^ link_length can include diags, if not careful...
        self._CFL_actives_prefactor = CFL_prefactor[self.grid.active_links]
        # ^note we can do this as topology shouldn't be changing
        dt_links = self._CFL_actives_prefactor / kd_links[
            self.grid.active_links]
        self.dt = np.nanmin(dt_links)

        # Get a list of interior cells
        self.interior_cells = self.grid.node_at_core_cell

        self.z = self.grid.at_node[self.values_to_diffuse]
        g = self.grid.zeros(at='link')
        qs = self.grid.zeros(at='link')
        try:
            self.g = self.grid.add_field('link', 'topographic__gradient', g,
                                         noclobber=True)
            # ^note this will object if this exists already
        except FieldError:
            self.g = self.grid.at_link['topographic__gradient']  # keep a ref
        try:
            self.qs = self.grid.add_field('link', 'unit_flux', qs,
                                          noclobber=True)
        except FieldError:
            self.qs = self.grid.at_link['unit_flux']
        # note all these terms are deliberately loose, as we won't always be
        # dealing with topo

        # do some pre-work to make fixed grad BC updating faster in the loop:
        self.updated_boundary_conditions()

        self._angle_of_link = self.grid.angle_of_link()
        self._vertlinkcomp = np.sin(self._angle_of_link)
        self._hozlinkcomp = np.cos(self._angle_of_link)

    def updated_boundary_conditions(self):
        """Call if grid BCs are updated after component instantiation.

        Sets `fixed_grad_nodes`, `fixed_grad_anchors`, & `fixed_grad_offsets`,
        such that::

            value[fixed_grad_nodes] = value[fixed_grad_anchors] + offset

        Examples
        --------
        >>> from landlab import RasterModelGrid
        >>> import numpy as np
        >>> mg = RasterModelGrid((4, 5), 1.)
        >>> z = mg.add_zeros('node', 'topographic__elevation')
        >>> z[mg.core_nodes] = 1.
        >>> ld = LinearDiffuser(mg, linear_diffusivity=1.)
        >>> ld.fixed_grad_nodes.size == 0
        True
        >>> ld.fixed_grad_anchors.size == 0
        True
        >>> ld.fixed_grad_offsets.size == 0
        True
        >>> mg.at_link['topographic__slope'] = mg.calc_grad_at_link(
        ...     'topographic__elevation')
        >>> mg.set_fixed_link_boundaries_at_grid_edges(True, True, True, True)
        >>> ld.updated_boundary_conditions()
        >>> ld.fixed_grad_nodes
        array([ 1,  2,  3,  5,  9, 10, 14, 16, 17, 18])
        >>> ld.fixed_grad_anchors
        array([ 6,  7,  8,  6,  8, 11, 13, 11, 12, 13])
        >>> ld.fixed_grad_offsets
        array([-1., -1., -1., -1., -1., -1., -1., -1., -1., -1.])
        >>> np.allclose(z[ld.fixed_grad_nodes],
        ...             z[ld.fixed_grad_anchors] + ld.fixed_grad_offsets)
        True
        """
        fixed_grad_nodes = np.where(self.grid.status_at_node ==
                                    FIXED_GRADIENT_BOUNDARY)[0]
        heads = self.grid.node_at_link_head[self.grid.fixed_links]
        tails = self.grid.node_at_link_tail[self.grid.fixed_links]
        head_is_fixed = np.in1d(heads, fixed_grad_nodes)
        self.fixed_grad_nodes = np.where(head_is_fixed, heads, tails)
        self.fixed_grad_anchors = np.where(head_is_fixed, tails, heads)
        vals = self.grid.at_node[self.values_to_diffuse]
        self.fixed_grad_offsets = (vals[self.fixed_grad_nodes] -
                                   vals[self.fixed_grad_anchors])

    def diffuse(self, dt, **kwds):
        """
        See :func:`run_one_step`.
        """
        if 'internal_uplift' in kwds.keys():
            raise KeyError('LinearDiffuser can no longer work with internal ' +
                           'uplift')
        z = self.grid.at_node[self.values_to_diffuse]

        core_nodes = self.grid.node_at_core_cell
        # do mapping of array kd here, in case it points at an updating
        # field:
        if type(self._kd) is np.ndarray:
            if not self._use_patch_kd:
                assert self._kd.size == self.grid.number_of_nodes
                kd_activelinks = self.grid.map_max_of_link_nodes_to_link(
                    self._kd)[self.grid.active_links]
            else:
                assert self._kd.size == self.grid.number_of_links
                kd_activelinks = self._kd[self.grid.active_links]
            # re-derive CFL condition, as could change dynamically:
            dt_links = self._CFL_actives_prefactor / kd_activelinks
            self.dt = np.nanmin(dt_links)
        else:
            if type(self._kd) is list:
                kd_activelinks = self.grid.map_max_of_link_nodes_to_link(
                    np.sqrt(self._kd[0]**2 + self._kd[1]**2))[
                        self.grid.active_links]
            else:
                kd_activelinks = self._kd
            # re-derive CFL condition, as could change dynamically:
            dt_links = self._CFL_actives_prefactor / kd_activelinks
            self.dt = np.nanmin(dt_links)

        # Take the smaller of delt or built-in time-step size self.dt
        self.tstep_ratio = dt/self.dt
        repeats = int(self.tstep_ratio//1.)
        extra_time = self.tstep_ratio - repeats

        # Can really get into trouble if no diffusivity happens but we run...
        if self.dt < np.inf:
            loops = repeats+1
        else:
            loops = 0
        for i in range(loops):
            if not self._use_patch_kd:
                # Calculate the gradients and sediment fluxes
                if self._use_patches:
                    (dzdx_at_patch,
                     dzdy_at_patch) = self.grid.calc_grad_at_patch(
                        self.values_to_diffuse)
                    num_patches_per_link = (
                        self.grid.patches_at_link != -1).sum(axis=1)
                    # map onto the links:
                    dzdx_at_link = (dzdx_at_patch[self.grid.patches_at_link] *
                                    self.grid.patches_present_at_link).sum(
                        axis=1)/num_patches_per_link  # a mean
                    dzdy_at_link = (dzdy_at_patch[self.grid.patches_at_link] *
                                    self.grid.patches_present_at_link).sum(
                        axis=1)/num_patches_per_link
                    # zero any with no patches:
                    no_patches = (num_patches_per_link == 0)
                    if np.any(no_patches):
                        dzdx_at_link[no_patches] = 0.
                        dzdy_at_link[no_patches] = 0.
                    vector_angle = np.arctan2(dzdy_at_link, dzdx_at_link)
                    resolving_fraction = np.cos(vector_angle -
                                                self._angle_of_link)
                    vector_mag = np.sqrt(dzdy_at_link**2 + dzdx_at_link**2)
                    np.multiply(resolving_fraction, vector_mag, out=self.g)
                else:
                    self.g[self.grid.active_links] = \
                            self.grid.calc_grad_at_link(z)[
                                self.grid.active_links]
                # if diffusivity is an array, self._kd is already
                # active_links-long
                self.qs[self.grid.active_links] = (
                    -kd_activelinks * self.g[self.grid.active_links])

                # Calculate the net deposition/erosion rate at each node
                self.dqsds = self.grid.calc_flux_div_at_node(self.qs)
            else:
                # print(self.grid.at_node['topographic__elevation'])
                (dzdx_at_patch,
                 dzdy_at_patch) = self.grid.calc_grad_at_patch(
                    self.values_to_diffuse)
                self.dzdx_at_patch = dzdx_at_patch
                self.dzdy_at_patch = dzdy_at_patch
                num_patches_per_link = (
                    self.grid.patches_at_link != -1).sum(axis=1)

                if type(self._kd) is not list:
                    # map onto the links:
                    dzdx_at_link = (dzdx_at_patch[self.grid.patches_at_link] *
                                    self.grid.patches_present_at_link).sum(
                        axis=1)/num_patches_per_link  # a mean
                    dzdy_at_link = (dzdy_at_patch[self.grid.patches_at_link] *
                                    self.grid.patches_present_at_link).sum(
                        axis=1)/num_patches_per_link
                    # zero any with no patches:
                    no_patches = (num_patches_per_link == 0)
                    if np.any(no_patches):
                        dzdx_at_link[no_patches] = 0.
                        dzdy_at_link[no_patches] = 0.
                    # hopefully here the minus signs resolve themselves...
                    qs_EW_at_link = -(np.fabs(self._kd * self._hozlinkcomp) *
                                      dzdx_at_link)
                    qs_NS_at_link = -(np.fabs(self._kd * self._vertlinkcomp) *
                                      dzdx_at_link)
                else:
                    # map the resolved diffusivities to the patches:
                    kd_x_at_patch = self.grid.map_mean_of_patch_nodes_to_patch(
                        self._kd[0])
                    kd_y_at_patch = self.grid.map_mean_of_patch_nodes_to_patch(
                        self._kd[1])
                    qs_EW_at_patch = -np.fabs(kd_x_at_patch)*dzdx_at_patch
                    qs_NS_at_patch = -np.fabs(kd_y_at_patch)*dzdy_at_patch
                    # map these to the links:
                    qs_EW_at_link = (
                        qs_EW_at_patch[self.grid.patches_at_link] *
                        self.grid.patches_present_at_link).sum(
                            axis=1)/num_patches_per_link  # a mean
                    qs_NS_at_link = (
                        qs_NS_at_patch[self.grid.patches_at_link] *
                        self.grid.patches_present_at_link).sum(
                            axis=1)/num_patches_per_link
                    # zero any with no patches:
                    no_patches = (num_patches_per_link == 0)
                    if np.any(no_patches):
                        qs_EW_at_link[no_patches] = 0.
                        qs_NS_at_link[no_patches] = 0.
                # now project the vector back onto the link. Only flux
                # parallel to the link can cross the face.
                vector_angle = np.arctan2(qs_NS_at_link, qs_EW_at_link)
                resolving_fraction = np.cos(vector_angle - self._angle_of_link)
                vector_mag = np.sqrt(qs_NS_at_link**2 + qs_EW_at_link**2)
                flux_along_link = resolving_fraction * vector_mag
                self.dqsds = self.grid.calc_flux_div_at_node(flux_along_link)

            # Calculate the total rate of elevation change
            dzdt = - self.dqsds
            # Update the elevations
            timestep = self.dt
            if i == (repeats):
                timestep *= extra_time
            else:
                pass
            self.grid.at_node[self.values_to_diffuse][core_nodes] += dzdt[
                core_nodes] * timestep

            # check the BCs, update if fixed gradient
            vals = self.grid.at_node[self.values_to_diffuse]
            vals[self.fixed_grad_nodes] = (vals[self.fixed_grad_anchors] +
                                           self.fixed_grad_offsets)

        return self.grid

    def run_one_step(self, dt, **kwds):
        """Run the diffuser for one timestep, dt.

        If the imposed timestep dt is longer than the Courant-Friedrichs-Lewy
        condition for the diffusion, this timestep will be internally divided
        as the component runs, as needed.

        Parameters
        ----------
        dt : float (time)
            The imposed timestep.
        """
        self.diffuse(dt, **kwds)

    @property
    def time_step(self):
        """Returns internal time-step size (as a property).
        """
        return self.dt
