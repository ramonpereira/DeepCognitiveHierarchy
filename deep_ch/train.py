"""
Main trainer function
"""
import theano
import numpy
import json
import sys
import time
import cPickle as pickle
import theano.tensor as tensor
from collections import OrderedDict
try:
    import bogota.data
except:
    print 'Bogota import failed...'

from projections import get_operator
from model import init_params, build_model, set_defaults
from utils import init_tparams, itemlist, floatx, zipp, unzip, print_params, split_params
from optim import get_optim
from data import encode_pool_list, filter_games

def state_file_name(options):
    nm = options.get('name', 'train_log')
    sd = options.get('seed', -99)
    fld = options.get('fold', -99)
    pth = options.get('path', './')
    filename = pth + nm + str(sd) + str(fld)
    return filename

def nll(x, y):
    return -tensor.sum(tensor.log(x) * y)

def load_par(options):
    filename = state_file_name(options) +'.pkl'
    par = pickle.load(open(filename))
    return par

def save_progress(options, tparams, epoch, best_perf):
    sd = options.get('seed', -99)
    fld = options.get('fold', -99)
    with open(state_file_name(options) +'.tmp', 'a') as w:
        w.write('%d,%d,%d,%f,%f,%f\n' % (epoch, sd, fld, 
            best_perf[0], best_perf[1], best_perf[2]))
    pickle.dump(unzip(tparams),
                open(state_file_name(options) +'.pkl', 'w'))

def resume_epoc(options):
    last_state = open(state_file_name(options) +'.tmp').readlines()[-1]
    return int(last_state.split(',')[0])

def apply_proximity(tparams, operators):
    '''
    Apply prox operator for proximal gradient to constrain parameters to required
    constraint set
    '''
    for op in operators:
        op_fn = get_operator(op)
        for key in operators[op]:
            # apply projection
            projected = op_fn(tparams[key].get_value())
            tparams[key].set_value(projected)

def train(options, data, load_params=False, start_epoc=0):
    print 'Setting up model with options:'
    options = set_defaults(options)
    check_interval = 1
    for kk, vv in options.iteritems():
        print kk, vv
    rng = numpy.random.RandomState(options['model_seed'] + 100*options.get('fold', 99) + options.get('seed',99))
    params, operators = init_params(options, rng)
    print 'done...'

    if load_params:
        loaded = load_par(options)
        start_epoc = resume_epoc(options)
        # Check that we've loaded the correct parameters...
        for kk, vv in loaded.iteritems():
            assert params[kk].shape == vv.shape
            assert type(params[kk]) == type(vv)
        params = loaded

    tparams = init_tparams(params)

    trng, use_noise, inps, out = build_model(tparams, options, rng)
    y = tensor.imatrix('y')
    obj = options.get('objective', 'nll')
    if obj == 'nll':
        cost = nll(out, y)
    elif obj == 'kl':
        cost = kl(out, y)
     
    f_eval = theano.function([inps, y], cost,
                             givens={use_noise: numpy.float32(0.)},
                             on_unused_input='ignore')

    reg = 0.
    for k, v in tparams.iteritems():
        if k[:6] == 'hidden' or k[-3:] == 'W_h':
            reg += options['l1'] * tensor.sum(abs(v))
            reg += options['l2'] * tensor.sum((v)**2)
    
    cost += reg

    grads = tensor.grad(cost, wrt=itemlist(tparams))
    lr = tensor.scalar(name='lr', dtype=theano.config.floatX)
    opt = get_optim(options['opt'])
    print 'Compiling functions'
    f_grad_shared, f_update, gshared = opt(lr, tparams, grads,
                                           [inps, y], cost, use_noise)
    f_out = theano.function([inps], out,
                            givens={use_noise: numpy.float32(0.)},
                            on_unused_input='ignore',
                            allow_input_downcast=True)

    best = numpy.inf
    print 'Starting training'

    train = list_update(data[0], f_eval, options['batch_size'], rng=rng)
    test = list_update(data[-1], f_eval, options['batch_size'], rng=rng)
    starting = (train, test)
    print 'Pre-training. test: %f, train: %f' % (test, train)

    print 'Training'
    lr = options['lr']
    min_lr = options['min_lr']
    max_lr = options['max_lr']
    max_itr = options['max_itr']
    grad_norm = 0.
    old_train = train
    check = 0 # when do we check for test performance
    train_scores = 50 * [0.]
    try:
        for epoch in xrange(max_itr):
            start_time = time.time()
            for g in gshared:
                g.set_value(0.0*g.get_value())
            use_noise.set_value(1.)
            train_cost, n_obs = list_update(data[0], f_grad_shared,batchsize=options['batch_size'], rng=rng,
                                            return_n_obs=True)
            use_noise.set_value(0.)
            for g in gshared:
                g.set_value(floatx(g.get_value() / float(n_obs)))
            par_old = unzip(tparams)
            f_update(lr)
            apply_proximity(tparams, operators)
            train = list_update(data[0], f_eval, options['batch_size'], rng=rng)
            if options['backtracking']:
                while train > old_train:
                    zipp(par_old, tparams)
                    lr *= 0.95
                    f_update(lr)
                    apply_proximity(tparams, operators)
                    train = list_update(data[0], f_eval)
                    if lr < min_lr:
                        break
                lr *= 1.1
                lr = numpy.clip(lr, min_lr, max_lr)

            old_train = train
            elapsed_time = time.time() - start_time
            
            if train < best:
                test = list_update(data[-1], f_eval)
                best_par = unzip(tparams)
                best_perf = (train, test)
                best = train
                check = 0

            check = (check + 1) % check_interval
            if check == 0:
                test = list_update(data[-1], f_eval)
            
            print 'Epoch: %d, cost: %f, train: %f, test: %f, lr:%f, time: %f' % (
                                                                    epoch,
                                                                    train_cost,
                                                                    train,
                                                                    test,
                                                                    lr,
                                                                    elapsed_time)

            if (epoch % 50) == 0:
                # Save progress....
                save_progress(options, tparams, epoch, best_perf)

            # Check if we're diverging...
            if epoch < 50:
                train_scores[epoch] = train
            else:
                train_scores.pop(0)
                train_scores.append(train)
                train_ave = sum(train_scores) / 50.
            if epoch > 1000:
                '''
                Only exit if we're diverging after 1000 iterations
                '''
                if train_ave > 1.03*best_perf[0]:
                    print "Diverged..."
                    break
    except KeyboardInterrupt:
        print "Interrupted"
    # check that we're outputing prob distributions
    X = data[0][(3, 3)][0]
    assert abs(f_out(X.reshape(X.shape[0], 2, 3, 3)).sum() - float(X.shape[0])) < 1e-4
    print "Best performance:"
    print "train, test"
    print "%f,%f" % best_perf
    return best_perf, best_par 


def sample_minibatch(data, batch_size=30, rng=numpy.random.RandomState(None)):
    games = []
    for d in data:
        for i in xrange(data[d][1].shape[0]):
            games.append((d,i))
    n = len(games)
    if n <= batch_size:
        return data
    batch = [games[g] for g in rng.permutation(n)[0:batch_size]]
    out = {}
    for g in batch:
        out[g[0]] = out.get(g[0], []) + [g[1]]
    for k, v in out.iteritems():
        idx = numpy.array(v)
        out[k] = (data[k][0][idx,:], data[k][1][idx])
    return out

def list_update(data, model, batchsize=None, f_update=None, return_n_obs=False, rng=numpy.random.RandomState(None)):
    if batchsize is not None:
        data = sample_minibatch(data, batchsize, rng)
    loss = 0.0
    y_tot = 0
    for i in data:
        n = data[i][1].shape[0]
        X = floatx(data[i][0].reshape(n, 2, i[0], i[1]))
        y = numpy.ndarray.astype(data[i][1], 'int32')
        y_tot += y.sum()
        loss += model(X, y)
        if f_update is not None:
            f_update(0.01)
    if return_n_obs:
        return loss, y_tot
    else:
        return loss


def get_data(dat, fold, normalise='pool', seed=187, nfolds=10, strat=False):
    train, test = dat.train_fold_gamewise(seed, nfolds, fold, True, stratified=strat)
    data = [encode_pool_list(train, normalise),
            encode_pool_list(test, normalise)]
    return data


DEFAULT_OPTIONS = {'name': 'test', 'feature_response':False, 'hidden': [50, 50],
               'col_hidden':[0], 'activ': 'relu',
               'dominance': True,
               'pooling': True,
               'batch_size': None,
               'col_activ': 'relu',
               'n_layers': 1, 'dropout': False, 'reduce':'sum',
               'opt_sharpness': False,
               'l1': 0.01, 'l2': 0.0, 'learning_rate': .5,
               'min_lr': 1e-4,'pooling_activ':'max', 'max_lr': 10., 'shared_ld': False,
               'opt': 'rmsprop', 'backtracking': False, 'ar_sharpen': 1.,
               'max_itr': 25000, 'lam_scaling': 100., 'simplex': True,
               'model_seed': 3, 'ar_features': False,
               'objective': 'nll'}

def main():
    if len(sys.argv) == 1:
        options = DEFAULT_OPTIONS
    else:
        options = json.load(open(sys.argv[1]))
        options['path'] = './test_runs/'
    print 'Getting Data'
    data = get_data(bogota.data.cn_all9, 0, normalise=50., seed=101)
    perf, par = train(options, data, False)
    for k, v in par.iteritems():
        print k
        print v


if __name__ == '__main__':
    main()