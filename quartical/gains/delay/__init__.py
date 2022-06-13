from quartical.gains.gain import Gain, gain_spec_tup, param_spec_tup
from quartical.gains.delay.kernel import delay_solver, delay_args
import numpy as np


class Delay(Gain):

    solver = delay_solver
    term_args = delay_args

    def __init__(self, term_name, term_opts, data_xds, coords, tipc, fipc):

        Gain.__init__(self, term_name, term_opts, data_xds, coords, tipc, fipc)

        parameterisable = ["XX", "YY", "RR", "LL"]

        self.parameterised_corr = \
            [ct for ct in self.corr_types if ct in parameterisable]
        self.n_param = 2 * len(self.parameterised_corr)

        self.gain_chunk_spec = gain_spec_tup(self.n_tipc_g,
                                             self.n_fipc_g,
                                             (self.n_ant,),
                                             (self.n_dir,),
                                             (self.n_corr,))
        self.param_chunk_spec = param_spec_tup(self.n_tipc_g,
                                               self.n_fipc_p,
                                               (self.n_ant,),
                                               (self.n_dir,),
                                               (self.n_param,))

        self.gain_axes = ("gain_t", "gain_f", "ant", "dir", "corr")
        self.param_axes = ("param_t", "param_f", "ant", "dir", "param")

    def make_xds(self):

        xds = Gain.make_xds(self)

        param_template = ["phase_offset_{}", "delay_{}"]

        param_labels = [pt.format(ct) for ct in self.parameterised_corr
                        for pt in param_template]

        xds = xds.assign_coords({"param": np.array(param_labels),
                                 "param_t": self.gain_times,
                                 "param_f": self.param_freqs})
        xds = xds.assign_attrs({"GAIN_SPEC": self.gain_chunk_spec,
                                "PARAM_SPEC": self.param_chunk_spec,
                                "GAIN_AXES": self.gain_axes,
                                "PARAM_AXES": self.param_axes})

        return xds

    @staticmethod
    def make_f_maps(chan_freqs, chan_widths, f_int):
        """Internals of the frequency interval mapper."""

        n_chan = chan_freqs.size

        # The leading dimension corresponds (gain, param). For unparameterised
        # gains, the parameter mapping is irrelevant.
        f_map_arr = np.zeros((2, n_chan,), dtype=np.int32)

        if isinstance(f_int, float):
            net_ivl = 0
            bin_num = 0
            for i, ivl in enumerate(chan_widths):
                f_map_arr[1, i] = bin_num
                net_ivl += ivl
                if net_ivl >= f_int:
                    net_ivl = 0
                    bin_num += 1
        else:
            f_map_arr[1, :] = np.arange(n_chan)//f_int

        f_map_arr[0, :] = np.arange(n_chan)

        return f_map_arr

    @staticmethod
    def init_term(
        gain, param, term_ind, term_spec, term_opts, ref_ant, **kwargs
    ):
        """Initialise the gains (and parameters)."""

        loaded = Gain.init_term(
            gain, param, term_ind, term_spec, term_opts, ref_ant, **kwargs
        )

        if loaded or not term_opts.initial_estimate:
            return

        data = kwargs["data"]  # (row, chan, corr)
        flags = kwargs["flags"]  # (row, chan)
        a1 = kwargs["a1"]
        a2 = kwargs["a2"]
        chan_freq = kwargs["chan_freqs"]
        t_map = kwargs["t_map_arr"][0, :, term_ind]  # time -> solint
        _, n_chan, n_ant, n_dir, n_corr = gain.shape
        # TODO: Make controllable/automate. Check with Landman.
        pad_factor = int(np.ceil(2 ** 15 / n_chan))

        # We only need the baselines which include the ref_ant.
        sel = np.where((a1 == ref_ant) | (a2 == ref_ant))
        a1 = a1[sel]
        a2 = a2[sel]
        t_map = t_map[sel]
        data = data[sel]
        flags = flags[sel]

        data[flags != 0] = 0

        utint = np.unique(t_map)

        for ut in utint:
            sel = np.where((t_map == ut) & (a1 != a2))
            ant_map_pq = np.where(a1[sel] == ref_ant, a2[sel], 0)
            ant_map_qp = np.where(a2[sel] == ref_ant, a1[sel], 0)
            ant_map = ant_map_pq + ant_map_qp
            ref_data = np.zeros((n_ant, n_chan, n_corr), dtype=np.complex128)
            counts = np.zeros((n_ant, n_chan), dtype=int)
            np.add.at(
                ref_data,
                ant_map,
                data[sel]
            )
            np.add.at(
                counts,
                ant_map,
                flags[sel] == 0
            )
            np.divide(
                ref_data,
                counts[:, :, None],
                where=counts[:, :, None] != 0,
                out=ref_data
            )

            fft_data = np.abs(
                np.fft.fft(ref_data, n=n_chan*pad_factor, axis=1)
            )
            fft_data = np.fft.fftshift(fft_data, axes=1)

            delta_freq = chan_freq[1] - chan_freq[0]
            fft_freq = np.fft.fftfreq(n_chan*pad_factor, delta_freq)
            fft_freq = np.fft.fftshift(fft_freq)

            delay_est_ind_00 = np.argmax(fft_data[..., 0], axis=1)
            delay_est_00 = fft_freq[delay_est_ind_00]

            if n_corr > 1:
                delay_est_ind_11 = np.argmax(fft_data[..., -1], axis=1)
                delay_est_11 = fft_freq[delay_est_ind_11]

            for t, p, q in zip(t_map[sel], a1[sel], a2[sel]):
                if p == ref_ant:
                    param[t, 0, q, 0, 1] = -delay_est_00[q]
                    if n_corr > 1:
                        param[t, 0, q, 0, 3] = -delay_est_11[q]
                else:
                    param[t, 0, p, 0, 1] = delay_est_00[p]
                    if n_corr > 1:
                        param[t, 0, p, 0, 3] = delay_est_11[p]

        coeffs00 = param[..., 1]*kwargs["chan_freqs"][None, :,  None, None]
        gain[..., 0] = np.exp(2j*np.pi*coeffs00)

        if n_corr > 1:
            coeffs11 = param[..., 3]*kwargs["chan_freqs"][None, :, None, None]
            gain[..., -1] = np.exp(2j*np.pi*coeffs11)
