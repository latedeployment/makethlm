#include <stdio.h>
#include "mylib.h"

int main(void) {
    printf("%s\n", greet("World"));
    printf("2 + 3 = %d\n", add(2, 3));
    return 0;
}
