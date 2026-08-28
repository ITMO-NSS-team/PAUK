"""Long work the panel schedules and a worker performs.

A publish takes minutes and a collection run takes hours, so neither can
happen inside an HTTP request. The panel writes a job document; a separate
process picks it up and calls the same functions `pauk` already calls from
the command line.
"""
