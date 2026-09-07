install:
	./instll/install

uninstall:
	./instll/uninstall

purge:
	TAP_PURGE=1 ./instll/uninstall
