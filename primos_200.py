#!/usr/bin/env python3
"""Calcula y muestra los primeros 200 números primos."""

# Determina si un número es primo.
def es_primo(n):
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    d = 3
    while d * d <= n:
        if n % d == 0:
            return False
        d += 2
    return True

# Genera los primeros 'cantidad' primos.
def generar_primos(cantidad):
    primos = []
    x = 2
    while len(primos) < cantidad:
        if es_primo(x):
            primos.append(x)
        x += 1
    return primos

if __name__ == '__main__':
    primos = generar_primos(200)
    print('Primeros 200 números primos:')
    for i,p in enumerate(primos,1):
        print(f'{i:3d}: {p}')
    print(f'\nTotal: {len(primos)}')
