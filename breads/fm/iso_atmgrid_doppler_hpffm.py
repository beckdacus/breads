import numpy as np
from PyAstronomy import pyasl
from astropy import constants as const
from scipy.interpolate import interp1d

from breads.utils import LPFvsHPF
from breads.utils import broaden
from breads.utils import broaden_kernel
from breads.utils import get_spline_model, pixgauss2d


# pos: (x,y) or fiber, position of the companion
def iso_atmgrid_doppler_hpffm(nonlin_paras, cubeobj, atm_grid=None, atm_grid_wvs=None, transmission=None,boxw=1, psfw=1.2,
             badpixfraction=0.75,hpf_mode=None,res_hpf=50,cutoff=5,fft_bounds=None,loc=None,N_nodes=3,fix_parameters=None):
    """
    For high-contrast companions (planet + speckles).
    Generate forward model removing the continuum with a fourier based high pass filter.

    Args:
        nonlin_paras: Non-linear parameters of the model, which are first the parameters defining the atmopsheric grid
            (atm_grid). The following parameters are the spin (vsini), the radial velocity, and the position (if loc is
            not defined) of the planet in the FOV.
                [atm paras ....,vsini,rv,y,x] for 3d cubes (e.g. OSIRIS)
                [atm paras ....,vsini,rv,y] for 2d (e.g. KPIC, y being fiber)
                [atm paras ....,vsini,rv] for 1d spectra
        cubeobj: Data object.
            Must inherit breads.instruments.instrument.Instrument.
        atm_grid: Planet atmospheric model grid as a scipy.interpolate.RegularGridInterpolator object. Make sure the
            wavelength coverage of the grid is just right and not too big as it will slow down the spin broadening.
        atm_grid_wvs: Wavelength sampling on which atm_grid is defined. Wavelength needs to be uniformly sampled.
        transmission: Transmission spectrum (tellurics and instrumental).
            np.ndarray of size the number of wavelength bins.
        boxw: size of the stamp to be extracted and modeled around the (x,y) location of the planet.
            Must be odd. Default is 1.
        psfw: Width (sigma) of the 2d gaussian used to model the planet PSF. This won't matter if boxw=1 however.
        badpixfraction: Max fraction of bad pixels in data.
        hpf_mode: choose type of high-pass filter to be used.
            "gauss": the data is broaden to the resolution specified by "res_hpf", which is then subtracted.
            "fft": a fft based high-pass filter is used using a cutoff frequency specified by "cutoff".
                This should not be used for (highly) non-uniform wavelength sampling or with gaps.
        res_hpf: float, if hpf_mode="gauss", resolution of the continuum to be subtracted.
        cutoff: int, if hpf_mode="fft", the higher the cutoff the more agressive the high pass filter.
            See breads.utils.LPFvsHPF().
        fft_bounds: [l1,l2,..ln] if hpf_mode is "fft", divide the spectrum into n chunks [l1,l2],..[..,ln] on which the
            fft high-pass filter is run separately.
        loc: Deprecated, Use fix_parameters.
            (x,y) position of the planet for spectral cubes, or fiber position (y position) for 2d data.
            When loc is not None, the x,y non-linear parameters should not be given.
        fix_parameters: List. Use to fix the value of some non-linear parameters. The values equal to None are being
                    fitted for, other elements will be fixed to the value specified.

    Returns:
        d: Data as a 1d vector with bad pixels removed (no nans)
        M: Linear model as a matrix of shape (Nd,1) with bad pixels removed (no nans). Nd is the size of the data
            vector.
        s: Noise vector (standard deviation) as a 1d vector matching d.
    """
    if fix_parameters is not None:
        _nonlin_paras = np.array(fix_parameters)
        _nonlin_paras[np.where(np.array(fix_parameters)==None)] = nonlin_paras
    else:
        _nonlin_paras = nonlin_paras

    if hpf_mode is None:
        hpf_mode = "gauss"
    Natmparas = len(atm_grid.values.shape)-1
    atm_paras = [p for p in _nonlin_paras[0:Natmparas]]
    other_nonlin_paras = _nonlin_paras[Natmparas::]

    # Handle the different data dimensions
    # Convert everything to 3D cubes (wv,y,x) for the followying
    if len(cubeobj.data.shape)==1:
        data = cubeobj.data[:,None,None]
        noise = cubeobj.noise[:,None,None]
        bad_pixels = cubeobj.bad_pixels[:,None,None]
    elif len(cubeobj.data.shape)==2:
        data = cubeobj.data[:,:,None]
        noise = cubeobj.noise[:,:,None]
        bad_pixels = cubeobj.bad_pixels[:,:,None]
    elif len(cubeobj.data.shape)==3:
        data = cubeobj.data
        noise = cubeobj.noise
        bad_pixels = cubeobj.bad_pixels
    if cubeobj.refpos is None:
        refpos = [0,0]
    else:
        refpos = cubeobj.refpos

    vsini,rv = other_nonlin_paras[0:2]
    # Defining the position of companion
    # If loc is not defined, then the x,y position is assume to be a non linear parameter.
    if np.size(loc) ==2:
        x,y = loc
    elif np.size(loc) ==1 and loc is not None:
        x,y = 0,loc
    elif loc is None:
        if len(cubeobj.data.shape)==1:
            x,y = 0,0
        elif len(cubeobj.data.shape)==2:
            x,y = 0,other_nonlin_paras[2]
        elif len(cubeobj.data.shape)==3:
            x,y = other_nonlin_paras[3],other_nonlin_paras[2]

    nz, ny, nx = data.shape
    if fft_bounds is None:
        fft_bounds = np.array([0,nz])

    # Handle the different dimensions for the wavelength
    # Only 2 cases are acceptable, anything else is undefined:
    # -> 1d wavelength and it is assumed to be position independent
    # -> The same shape as the data in which case the wavelength at each position is specified and can bary.
    if len(cubeobj.wavelengths.shape)==1:
        wvs = cubeobj.wavelengths[:,None,None]
    elif len(cubeobj.wavelengths.shape)==2:
        wvs = cubeobj.wavelengths[:,:,None]
    elif len(cubeobj.wavelengths.shape)==3:
        wvs = cubeobj.wavelengths
    _, nywv, nxwv = wvs.shape

    if boxw % 2 == 0:
        raise ValueError("boxw, the width of stamp around the planet, must be odd in splinefm().")
    if boxw > ny or boxw > nx:
        raise ValueError("boxw cannot be bigger than the data in splinefm().")


    # remove pixels that are bad in the transmission or the star spectrum
    bad_pixels[np.where(np.isnan(transmission))[0],:,:] = np.nan

    # Extract stamp data cube cropping at the edges
    w = int((boxw - 1) // 2)
    # Number of linear parameters
    N_linpara = N_nodes # planet flux

    _paddata =np.pad(data,[(0,0),(w,w),(w,w)],mode="constant",constant_values = np.nan)
    _padnoise =np.pad(noise,[(0,0),(w,w),(w,w)],mode="constant",constant_values = np.nan)
    _padbad_pixels =np.pad(bad_pixels,[(0,0),(w,w),(w,w)],mode="constant",constant_values = np.nan)
    k, l = int(np.round(refpos[1] + y)), int(np.round(refpos[0] + x))
    dx,dy = x-l+refpos[0],y-k+refpos[1]
    padk,padl = k+w,l+w

    # high pass filter the data
    cube_stamp = _paddata[:, padk-w:padk+w+1, padl-w:padl+w+1]
    badpix_stamp = _padbad_pixels[:, padk-w:padk+w+1, padl-w:padl+w+1]
    badpixs = np.ravel(badpix_stamp)
    s = np.ravel(_padnoise[:, padk-w:padk+w+1, padl-w:padl+w+1])
    badpixs[np.where(s==0)] = np.nan

    where_finite = np.where(np.isfinite(badpixs))

    if np.size(where_finite[0]) <= (1-badpixfraction) * np.size(badpixs) or vsini < 0 or \
            padk > ny+2*w-1 or padk < 0 or padl > nx+2*w-1 or padl < 0:
        # don't bother to do a fit if there are too many bad pixels
        return np.array([]), np.array([]).reshape(0,N_linpara), np.array([])
    else:

        planet_model = atm_grid(atm_paras)[0]

        if np.sum(np.isnan(planet_model)) >= 1 or np.sum(planet_model)==0 or np.size(atm_grid_wvs) != np.size(planet_model):
            return np.array([]), np.array([]).reshape(0,N_linpara), np.array([])

        dirac = np.zeros(np.size(atm_grid_wvs))
        midpoint = np.size(atm_grid_wvs)//2
        dirac[midpoint] = 1
        broaden_dirac = pyasl.fastRotBroad(atm_grid_wvs, dirac, 0.1, vsini,effWvl=atm_grid_wvs[midpoint])
        where_kernel = np.where(broaden_dirac != 0)
        dirac_wvs = (atm_grid_wvs - atm_grid_wvs[midpoint]) / atm_grid_wvs[midpoint]

        x_knots = np.linspace(dirac_wvs[where_kernel[0][0]],dirac_wvs[where_kernel[0][-1]], N_linpara, endpoint=True).tolist()
        M_spline = get_spline_model(x_knots, dirac_wvs, spline_degree=3)*broaden_dirac[:,None]
        # import matplotlib.pyplot as plt
        # plt.plot(np.nansum(M_spline,axis=1))
        # plt.plot(broaden_dirac,"--")
        # plt.show()
        planet_f_list = []
        sum_broad_model = np.zeros(np.size(planet_model))
        for node_id in  range(N_linpara):
            kernel = interp1d(dirac_wvs,M_spline[:,node_id], bounds_error=False, fill_value=0)
            broad_model = broaden_kernel(atm_grid_wvs, planet_model,kernel)
            sum_broad_model = sum_broad_model+broad_model
            planet_f = interp1d(atm_grid_wvs,broad_model, bounds_error=False, fill_value=0)
            planet_f_list.append(planet_f)
        # import matplotlib.pyplot as plt
        # plt.plot(sum_broad_model)
        # plt.plot(pyasl.fastRotBroad(atm_grid_wvs, planet_model, 0.1, vsini,effWvl=atm_grid_wvs[midpoint]),"--")
        # plt.show()

        psfs = np.zeros((nz, boxw, boxw))
        # Technically allows super sampled PSF to account for a true 2d gaussian integration of the area of a pixel.
        # But this is disabled for now with hdfactor=1.
        hdfactor = 1#5
        xhdgrid, yhdgrid = np.meshgrid(np.arange(hdfactor * (boxw)).astype(np.float) / hdfactor,
                                       np.arange(hdfactor * (boxw)).astype(np.float) / hdfactor)
        psfs += pixgauss2d([1., w+dx, w+dy, psfw, 0.], (boxw, boxw), xhdgrid=xhdgrid, yhdgrid=yhdgrid)[None, :, :]
        psfs = psfs / np.nansum(psfs, axis=(1, 2))[:, None, None]

        # Stamp cube that will contain the data
        data_hpf = np.zeros((nz,boxw,boxw))+np.nan
        data_lpf = np.zeros((nz,boxw,boxw))+np.nan
        # Stamp cube that will contain the planet model
        scaled_psfs_hpf = np.zeros((nz,boxw,boxw,N_linpara))+np.nan

        # Loop over each spaxel in the stamp cube (boxw,boxw)
        for paraid,planet_f in enumerate(planet_f_list):
            for _k in range(boxw):
                for _l in range(boxw):
                    lwvs = wvs[:,np.clip(k-w+_k,0,nywv-1),np.clip(l-w+_l,0,nxwv-1)]

                    # The planet spectrum model is RV shifted and multiplied by the tranmission
                    # Go from a 1d spectrum to the 3D scaled PSF
                    planet_spec = transmission * planet_f(lwvs * (1 - (rv - cubeobj.bary_RV) / const.c.to('km/s').value))
                    scaled_vec = psfs[:, _k,_l] * planet_spec

                    # High pass filter the data and the models
                    if hpf_mode == "gauss":
                        if paraid == 0:
                            data_lpf[:,_k,_l] = broaden(lwvs,cube_stamp[:,_k,_l]*badpix_stamp[:,_k,_l],res_hpf)
                            data_hpf[:,_k,_l] = cube_stamp[:,_k,_l]-data_lpf[:,_k,_l]

                        scaled_vec_lpf = broaden(lwvs,scaled_vec*badpix_stamp[:,_k,_l],res_hpf)
                        scaled_psfs_hpf[:,_k,_l,paraid] = scaled_vec-scaled_vec_lpf
                    elif hpf_mode == "fft":
                        for lb,rb in zip(fft_bounds[0:-1],fft_bounds[1::]):
                            if paraid == 0:
                                data_lpf[lb:rb, _k, _l],data_hpf[lb:rb,_k,_l] = LPFvsHPF(cube_stamp[lb:rb,_k,_l]*badpix_stamp[lb:rb,_k,_l],cutoff)

                            _,scaled_psfs_hpf[lb:rb,_k,_l,paraid] = LPFvsHPF(scaled_vec[lb:rb]*badpix_stamp[lb:rb,_k,_l],cutoff)

        d = np.ravel(data_hpf)

        # combine planet model with speckle model
        # M = np.concatenate([scaled_psfs_hpf[:, :, :, None], M_speckles_hpf[:, :, :, None]], axis=3)
        M = scaled_psfs_hpf[:, :, :, None]
        # Ravel data dimension
        M = np.reshape(M, (nz * boxw * boxw, N_linpara))
        # Get rid of bad pixels
        sr = s[where_finite]
        dr = d[where_finite]
        Mr = M[where_finite[0], :]

        return dr, Mr, sr